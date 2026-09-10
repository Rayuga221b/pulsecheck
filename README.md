# pulsecheck

**Monitoring & auto-recovery pipeline for a simulated market-data service.**

A small FastAPI service emits synthetic price ticks into Postgres and Redis and
exposes Prometheus metrics. Around it sit three host-side tools that watch it the
way a first-level on-call would from *outside* the containers:

- **Prometheus + Grafana** — scrape `/metrics` every 5s; one hand-built dashboard.
- **log-watcher** — a stdlib Python tailer of the service's JSON log ("grep with
  memory"): windowed rules for error bursts, repeated connection failures and
  known failure strings.
- **watchdog** — polls `/health` *and* the container's Docker state; after `N`
  consecutive failed checks it restarts the container, writes an incident row to
  SQLite, and posts a Slack alert. One healthy poll closes the incident.

Everything is rule-based and explainable — no ML, no Alertmanager, no log
platform, no Kubernetes. The design choices are deliberate; see the "Why it's
built this way" notes at the end.

---

## Architecture

```
Host (Docker Compose — the same compose file runs locally and, later, on one EC2 box)

  ┌────────────────────────────── containers ──────────────────────────────┐
  │                                                                        │
  │   feed-service (FastAPI, 1 worker)  ──INSERT──▶  postgres  (tick history, named volume)
  │     GET /health   200 iff BOTH deps round-trip, else 503 + JSON reason  │
  │     GET /metrics  3 custom Prometheus series     ──SET latest:<sym>──▶  redis
  │        │                                                               │
  │        │ one JSON object per line                 prometheus  ──scrape /metrics every 5s
  │        ▼                                              │                 │
  │   ./logs/feed-service.log  (bind mount)               ▼                 │
  │        │                                          grafana  (provisioned datasource + dashboard)
  └────────┼───────────────────────────────────────────────────────────────┘
           │                        ▲
   host    │ tail + regex/window    │ Docker API (state) + HTTP /health poll
 processes ▼                        │
   log-watcher ──┐         watchdog ─┴─ consecutive-breach state machine
                 │            ├─▶ docker restart <container>
                 ▼            ├─▶ incidents.db (SQLite): detected_at, component,
        incidents.db          │                 detected_via, symptom, action_taken, resolved_at
        (shared schema,       └─▶ Slack incoming webhook (symptom / action / downtime)
         incidents.py)
```

`watchdog` and `log-watcher` run **on the host, not in containers** — they need
the Docker socket and a host-reachable `/health`, and "observe the containers
from outside" is the realistic L1 posture.

| Component | Path | What it is |
|---|---|---|
| feed-service | [`feed-service/`](feed-service/) | FastAPI app: random-walk ticks → Postgres + Redis; `/health`, `/metrics`, JSON logs. |
| compose stack | [`docker-compose.yml`](docker-compose.yml) | `feed-service`, `postgres`, `redis`, `prometheus`, `grafana` on one bridge network. |
| Prometheus | [`prometheus/prometheus.yml`](prometheus/prometheus.yml) | One static 5s scrape job → `feed-service:8000`, plus a self-scrape. |
| Grafana | [`grafana/`](grafana/) | Provisioned datasource + one 3-panel dashboard (checked in as JSON). |
| log-watcher | [`log_watcher/`](log_watcher/) | Stdlib polling tailer + windowed detection rules → `incidents` rows. |
| watchdog | [`watchdog/`](watchdog/) | `/health` + container-state poll, consecutive-breach logic, restart + incident + Slack. |
| shared schema | [`incidents.py`](incidents.py) | The single `CREATE TABLE` for `incidents.db`, imported by both host tools. |
| fault injection | [`scripts/induce_failure.sh`](scripts/induce_failure.sh) | `kill` / `cpu` / `netlat` / `clear` — three failure shapes, each caught by a different signal. |

### The three feed-service metrics

| Metric | Type | Why that type |
|---|---|---|
| `ingestion_lag_seconds` | **Gauge** | Seconds since the last successful tick write. Moves both directions (→0 after a write, climbs when writes stop). Computed at *scrape time* so it keeps rising even if the generator thread is wedged. |
| `ticks_processed_total` | **Counter** | Monotonic count of ticks written. Graphed as `rate(ticks_processed_total[1m])` for ticks/sec; Prometheus handles the reset-to-0 on restart. |
| `request_latency_seconds` | **Histogram** | Latency of the write path. Bucketed in-process so Prometheus can do `histogram_quantile(0.95, …)` server-side and aggregate across instances (a Summary could not). |

---

## Prerequisites

- **Docker + Compose.** Either `docker compose` (v2 plugin) or the standalone
  `docker-compose` binary works — examples below use `docker-compose`.
  [Colima](https://github.com/abiosoft/colima) is fine as the Docker backend on
  macOS (set `DOCKER_HOST`, see `.env.example`).
- **Python 3.12** on the host for the watchdog and log-watcher.
- A **Slack incoming webhook URL** if you want real alerts (optional — without
  it the watchdog still runs, logs, and records incidents; it just skips the POST).

---

## Setup

```bash
# 1. Config: copy the template and edit values (real Postgres password, Slack URL).
cp .env.example .env
$EDITOR .env

# 2. Bring up the container stack.
docker-compose up -d --build

# 3. Host tools — each is its own venv (the repo has no monorepo tooling).
python3.12 -m venv watchdog/.venv
watchdog/.venv/bin/pip install -r watchdog/requirements.txt
# log-watcher has NO third-party dependencies — stdlib only, no venv needed.
```

Run the two host tools in their own terminals, **from the repo root**:

```bash
# terminal A — watchdog (Docker socket + host /health poll)
watchdog/.venv/bin/python -m watchdog

# terminal B — log-watcher (tails ./logs/feed-service.log)
python3 -m log_watcher
```

---

## Verify it's working

```bash
# /health — 200 with both dependencies ok
curl -s localhost:8000/health
# {"status":"ok","postgres":"ok","redis":"ok"}

# /metrics — the three custom series are present
curl -s localhost:8000/metrics | grep -E '^# TYPE (ingestion_lag_seconds|ticks_processed_total|request_latency_seconds)'

# ticks are landing in Postgres
docker exec pulsecheck-postgres psql -U pulsecheck -d pulsecheck -c 'SELECT count(*) FROM ticks;'

# latest price per symbol is in Redis
docker exec pulsecheck-redis redis-cli --scan --pattern 'latest:*'
```

- **Prometheus** → <http://localhost:9090/targets> — both targets `UP`.
- **Grafana** → <http://localhost:3000> — opens anonymously on the **pulsecheck**
  dashboard: ingestion lag, request-latency p95, ticks/sec, all showing live data.

Quick negative check — dependency down is a *reasoned* 503, not a hang:

```bash
docker-compose stop redis
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/health   # 503
curl -s localhost:8000/health   # {"status":"unhealthy","postgres":"ok","redis":"error: ..."}
docker-compose start redis      # app self-heals via backoff; no restart needed
```

---

## Demo walkthrough — induce a failure, watch the pipeline react

With the stack up and **both** host tools running, run one scenario at a time
from the repo root. Each prints what it simulates, which signal should catch it,
and the rough detection latency before it acts.

```bash
scripts/induce_failure.sh kill              # hard crash  (SIGKILL -> container leaves 'running')
scripts/induce_failure.sh netlat 600 40     # +600ms NIC latency for 40s (/health poll times out)
scripts/induce_failure.sh cpu 40            # peg every core for 40s (degradation, usually NOT a restart)
scripts/induce_failure.sh clear             # remove any leftover tc netem qdisc
```

### `kill` — hard crash → caught by the **container** probe

| Where | What you should see |
|---|---|
| `docker ps` | `pulsecheck-feed-service` disappears, then reappears seconds later. |
| watchdog log | `breach 1/3`, `breach 2/3` (no action) → `failure CONFIRMED via container: container state is 'exited' (exit code 137)` → `restarted container pulsecheck-feed-service` → on the next healthy poll, `RECOVERED after ~Ns`. |
| `incidents.db` | one new row: `detected_via=container`, `symptom` = the exit-code text, `action_taken='restarted container …'`, `resolved_at` filled in on recovery. |
| Slack | 🚨 incident message (symptom / detected-via / action / time), then ✅ recovery message with downtime. |
| Grafana | a gap in ticks/sec and a spike in `ingestion_lag_seconds`, then recovery. |

Detection latency ≈ `WATCHDOG_POLL_SECONDS × BREACH_THRESHOLD` ≈ **6s** (connection
refused returns instantly, so each failed poll is cheap).

### `netlat` — slow network → caught by the **health** probe, *indirectly*

The watchdog has no "slow" signal. Each `/health` poll now exceeds its 3s timeout,
and a timed-out poll counts as a failed check. Same state machine, same restart —
but slower to confirm (**~15s**): every failed poll now burns its full timeout
before the next one starts. The incident row's `detected_via` is `health`. The
restart recreates the veth, so the `tc` netem qdisc vanishes with it (the script
also deletes it on exit).

### `cpu` — CPU saturation → **degradation, not a restart** (by design)

`/health` is I/O-bound (it waits on Postgres/Redis sockets and yields the CPU),
so on a multi-core box it still answers inside the timeout and the watchdog
correctly does **nothing**. The symptom shows up on **Grafana** — request-latency
p95 and `ingestion_lag_seconds` climb — which is the honest outcome: a
timeout-based liveness check is meant to catch "can't respond", not "slow". On a
CPU-constrained box (a 2-vCPU instance with a container CPU limit) a hard enough
spike *will* cross the timeout and escalate.

### log-watcher path — dependency outage

The three faults above don't exercise the log-watcher (an instant kill logs
nothing; the latency fault doesn't sustain error lines long enough). Its path is
a **dependency outage**:

```bash
docker-compose stop redis      # feed-service starts logging tick_write_error + dependency_retry
# log-watcher stdout:  [ts] incident #N via error_burst: error burst: 5 ERROR log lines within 20s …
#                      [ts] incident #N via connection_failures: 3 retry attempts within 60s …
docker-compose start redis
```

Each rule writes one `incidents` row (`detected_via=log-watcher`) and then cools
down, so one sustained outage is one row per rule, not hundreds.

Inspect incidents at any time:

```bash
sqlite3 incidents.db 'SELECT id, detected_at, component, detected_via, action_taken, resolved_at FROM incidents ORDER BY id DESC LIMIT 10;'
```

---

## Tests and linting

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt \
                      -r feed-service/requirements.txt \
                      -r watchdog/requirements.txt
.venv/bin/ruff check .
.venv/bin/pytest
```

- **`feed-service/tests/`** — `/health` returns 200 only when both dependencies
  round-trip, and 503 + a JSON reason when either is down; `/metrics` exposes the
  three series with the right Prometheus types. Uses `TestClient` without the
  lifespan, so no real Postgres/Redis is touched.
- **`watchdog/tests/`** — the consecutive-breach state machine (`breach.py`),
  driven entirely by fake verdicts and injected timestamps: a sub-threshold blip
  never acts, `N` consecutive failures confirm on the Nth, one success resets the
  count, a confirmed incident doesn't re-fire, recovery is asymmetric and reports
  downtime.
- **`log_watcher/tests/`** — the windowed rules: fire on the Nth match in the
  window (not the (N-1)th), cooldown collapses a sustained outage into one
  detection, and a replay of the committed `sample_incident.log` yields exactly
  one row per rule.

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs the same
`ruff` + `pytest`, then builds the feed-service image, scans it with **Trivy**
(fails on fixable `CRITICAL`s), and — only on a push to `dev`/`main` — pushes it
to Docker Hub using the `DOCKERHUB_USERNAME` / `DOCKERHUB_TOKEN` repo secrets. No
credentials are committed.

---

## Configuration

All runtime configuration is in `.env` (gitignored); `.env.example` is the
committed, fully-commented template. Every value the code reads lives there — no
magic numbers in source. The knobs most worth knowing:

| Variable | Default | Meaning |
|---|---|---|
| `TICK_INTERVAL_SECONDS` | `0.25` | Per-symbol tick cadence (4/s/symbol). |
| `SYMBOLS` | `AAPL,GOOG,MSFT,AMZN` | Symbols to walk. |
| `BREACH_THRESHOLD` | `3` | Consecutive failed watchdog checks before it acts. |
| `WATCHDOG_POLL_SECONDS` | `2.0` | Seconds between watchdog polls. |
| `WATCHDOG_HEALTH_TIMEOUT_SECONDS` | `3.0` | Per-poll HTTP timeout = ceiling on a hung poll. |
| `WATCHDOG_RECOVERY_CONFIRMATIONS` | `1` | Healthy polls needed to close an incident. |
| `SLACK_WEBHOOK_URL` | — | Incoming-webhook URL; unset ⇒ alerts skipped. |
| `LOG_WATCHER_ERROR_BURST_COUNT` / `_WINDOW_SECONDS` | `5` / `20` | Error-burst rule. |
| `LOG_WATCHER_CONN_FAIL_COUNT` / `_WINDOW_SECONDS` | `3` / `60` | Repeated-retry rule. |
| `DOCKER_HOST` | unset | Set to the Colima socket locally; leave unset on a native Docker host. |

---

## Deployment to AWS — *planned, not yet executed*

The intended target is **one** free-tier `t3.micro` EC2 instance (Ubuntu LTS)
running the **unchanged** `docker-compose.yml` — a deploy-target change, not a
redesign. Outline:

1. Launch the instance; install `docker.io` + `docker-compose-plugin`; restricted SSH key pair.
2. Security group scoped to a single source IP `/32`: 22 (SSH), 3000 (Grafana),
   optionally 8000. Postgres 5432 and Redis 6379 are **never** exposed — bridge
   network only.
3. Instance **IAM role** with least-privilege S3 write to one backup bucket
   (not `s3:*`); the watchdog uses `boto3`'s default credential chain — no static keys.
4. One S3 bucket; the watchdog pushes incident JSON / `incidents.db` there after
   each incident (SQLite on the instance is ephemeral relative to the instance).
5. `git clone` + `docker-compose up -d` on the box; re-run the induced-failure
   chain against the remote instance.

This section will move from "planned" to "done" when that work lands.

---

## Why it's built this way (short version)

- **watchdog uses "N consecutive failures", not the first failed check** — a
  single failed probe (GC pause, one TCP hiccup, a mid-recreate poll) is a weak
  signal, and a container restart is a big hammer (dropped writes, cold pools).
  The trade-off is ~6s of detection latency, which is fine for an L1 aid.
- **Recovery is asymmetric** — `N` failures to open an incident, `1` success to
  close it. A false "it's down" costs a restart; a false "it's back" just closes
  the row a little early and the next bad poll re-opens it.
- **`/health` checks readiness, not liveness** — 200 only if Postgres *and* Redis
  round-trip. "The process is running" (container state / `/`) is a separate,
  weaker signal; the watchdog reads both and records which one failed.
- **Two stores on purpose** — Postgres for durable tick *history* (relational,
  range queries, must survive restart); Redis for *latest price per symbol*
  (one key, read constantly, fine to lose on restart).
- **The host tools are stdlib-first** — the log-watcher has zero dependencies;
  the watchdog has exactly one (`docker`). Fewer things to install and patch on
  the box, and every line is whiteboard-explainable.
