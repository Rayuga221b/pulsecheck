# CLAUDE.md — pulsecheck

Monitoring & Auto-Recovery Pipeline for a Simulated Market-Data Service.

## What this project is (read before every session)

A resume / interview-prep project by a 4th-year CS student targeting **DevOps L1** roles
(target: Quantbox, a prop trading firm; generally applicable elsewhere).

**Guiding principle: depth over breadth.** Every component must be something the author can
explain from first principles in an interview — not "I deployed it and it worked." When in
doubt, choose the version that is easier to explain line-by-line, even if it is less "clever."

### Implementation priorities (in order)

1. Code the author can read and explain line-by-line — avoid clever/compressed code.
2. Comments explain **why**, not just what.
3. Small, testable pieces over one large script.
4. Realistic thresholds/values with stated justification — no unexplained magic numbers.

## Hard scope boundaries — DO NOT add these unless explicitly asked

These were deliberately cut during scoping to keep the project defensible. Do not suggest
adding them back without being asked.

- **No Kubernetes** — stay on Docker Compose.
- **No Alertmanager** — the watchdog sends the Discord webhook directly.
- **No Loki / Promtail** — log-watcher is a plain Python regex tailer.
- **No ML / anomaly-detection library** anywhere — all failure detection is rule-based and explainable.
- **No real external market-data API** — synthetic in-process tick generation is intentional and sufficient.
- **No ECS / EKS / Fargate** — deploy target is a single plain EC2 instance running Docker Compose.
- **No RDS** — Postgres stays containerized on the same instance.
- **No ALB / Route53 / CloudFront** — no functional need.
- **No CloudWatch** — Prometheus/Grafana already cover this; adding it is tool-stacking, not depth.
- **No static AWS credentials** anywhere in code, scripts, or commit history — IAM role only.

If a proposed feature doesn't serve an interview-readiness checklist item (below) or core JD
alignment (monitoring, first-level debugging, log analysis, Python/Bash scripting, networking
fundamentals, incident escalation), don't build it.

## Architecture

```
Host (Docker Compose) — same compose file runs locally and on EC2

  feed-service (FastAPI)  ──▶  postgres (tick store)
    /metrics  /health     ──▶  redis (latest price per symbol)
        │
        │ JSON logs to file            custom bridge network
        ▼                              Prometheus scrapes /metrics every 5s
  log-watcher (host Python,            │
   regex tailer)                       ▼
        │                        Prometheus ──▶ Grafana (provisioned dashboard)
        ▼
  watchdog (host Python, Docker SDK + /health poll)
        │  consecutive-breach logic
        ├─▶ restart container
        ├─▶ incidents.db (SQLite): timestamp, component, detected_via, action_taken, resolved_at
        ├─▶ Discord webhook (symptom, action taken, duration)
        └─▶ [AWS] push incident JSON / incidents.db to S3 via boto3 (instance IAM role)
```

- **watchdog and log-watcher run on the host, not containerized** — simpler to explain and
  debug, and realistic for an L1 tool that observes containers from the outside.
- CI/CD: GitHub Actions — lint → test → Trivy scan → build → push → (manual or SSH deploy to EC2).

## Component build order

Build strictly in this order. Do not start AWS (section 10) until 1–9 work locally and the
induced-failure demo has been run successfully at least once locally.

| # | Path | Summary |
|---|------|---------|
| 1 | `feed-service/` | FastAPI app: synthetic random-walk ticks at configurable interval → Postgres `ticks` table (symbol, price, timestamp) + latest price per symbol in Redis. `/health` returns 200 only if **both** Postgres and Redis are reachable, else 503 + JSON reason. `/metrics` via `prometheus_client` with **exactly three** custom metrics. Every tick and error logged as single-line JSON to a **file** (fields: `level`, `event`, `detail`). |
| 2 | `docker-compose.yml` | Services: `feed-service`, `postgres`, `redis`, `prometheus`, `grafana`. Custom bridge network; named volumes for Postgres data and Grafana dashboards. Watchdog + log-watcher stay as host processes. |
| 3 | `prometheus/prometheus.yml` | Single static scrape job → `feed-service:8000/metrics`, 5s interval. No service discovery. |
| 4 | `grafana/` | One hand-built dashboard (not imported), three panels: ingestion lag; request latency p95 via `histogram_quantile`; ticks/sec via `rate()`. Provisioned as JSON in the repo so it loads on `docker-compose up`. |
| 5 | `log_watcher/` | Standalone Python. Simple polling tail of the feed-service JSON log. Regex/field rules for: repeated connection failures, error bursts (N errors in a rolling window), known failure strings. On match → stdout + incidents table. "grep with memory," not a log platform. |
| 6 | `watchdog/` | **Core of the project — most heavily commented file in the repo.** Polls `/health` AND container status via Docker SDK (`docker-py`). Consecutive-breach logic: act only after N consecutive failed checks, not a single blip (comment the anti-flapping reasoning). On confirmed failure: restart container, write incident row to `incidents.db`, send Discord webhook. |
| 7 | `scripts/induce_failure.sh` | Documented one-liners: `docker kill` feed-service; CPU spike (`stress --cpu`); network latency (`tc`). Each with a one-line comment: what it simulates + which signal should catch it. |
| 8 | `.github/workflows/ci.yml` | lint (`ruff` or `flake8`) → `pytest` (feed-service endpoints + watchdog consecutive-breach logic) → Trivy scan on built image → build → push to Docker Hub (repo secrets, no hardcoded creds). |
| 9 | `README.md` | Architecture diagram, setup instructions, demo walkthrough (run `induce_failure.sh`, what to expect in Grafana / Discord / incidents DB), AWS deployment steps, how the demo differs on EC2 vs local. |

### The three feed-service metrics (do not add more)

- `ingestion_lag_seconds` — **Gauge** — time since last successful tick write.
- `ticks_processed_total` — **Counter** — total ticks ingested.
- `request_latency_seconds` — **Histogram** — latency of the ingestion write path.

## Section 10 — AWS deployment (only after 1–9 stable locally)

Deploy target: **one** `t2.micro`/`t3.micro` EC2 instance (free tier), Ubuntu LTS. Same
`docker-compose.yml`, unchanged. This is a deploy-target change, not a redesign.

- **10a** — Launch EC2, Ubuntu LTS AMI. SSH key pair with restricted access. Install
  `docker.io` + `docker-compose-plugin`, add user to `docker` group.
- **10b** — Security group, scoped deliberately: 22 (SSH), 3000 (Grafana), and optionally
  8000 (feed-service) each from **your IP /32 only**; 8000 closed if no external access needed.
  Postgres 5432 and Redis 6379 **never exposed** — bridge network only. Be able to justify
  every rule (attack-surface minimization) and state the risk of `0.0.0.0/0`.
- **10c** — IAM role attached to the instance with **least-privilege S3 write** scoped to the
  one backup bucket (not `s3:*`). Watchdog uses `boto3`'s default credential chain — no
  hardcoded keys anywhere.
- **10d** — One S3 bucket for incident backups. Watchdog pushes incident JSON (or full
  `incidents.db`) to S3 after each incident. Comment: SQLite on the instance is ephemeral
  relative to the instance lifecycle; S3 gives durable storage independent of the instance.
- **10e** — `git clone` repo to instance, `docker-compose up -d`, re-verify the full
  induced-failure chain runs against the remote instance.
- **10f** — *(optional, time-permitting, only after 10a–10e work manually)* CI-triggered
  deploy: final GitHub Actions step SSHes into EC2 after image push, runs
  `docker-compose pull && docker-compose up -d`. SSH key + host as Actions secrets, never
  committed. If time is short, skip it and document honestly: "CI builds and pushes the
  image; deploy to EC2 is currently manual; CI-triggered deploy is a natural next step."

## Interview-readiness checklist — the author must be able to explain, unprompted

- Why gauge vs. counter vs. histogram for each of the three metrics.
- What happens if Prometheus scrapes mid-crash (staleness handling).
- Why consecutive-breach detection instead of single-spike triggers in the watchdog.
- What `/health` actually verifies and why that differs from "process is running."
- What changes at the TCP level when `tc` adds network delay, and how the watchdog notices it
  indirectly (timeout-based, not a signal).
- Why each security-group rule is scoped the way it is, and the actual risk of `0.0.0.0/0`.
- Why an instance IAM role instead of static access keys, and the risk of hardcoded credentials.
- What happens to the local SQLite incident log if the EC2 instance is stopped/restarted, and
  why that is the reason S3 backup was added.

## Progress tracker

Update this section as work lands. `[ ]` todo · `[~]` in progress · `[x]` done.

- [x] Repo created, git initialized, pushed to GitHub (`Rayuga221b/pulsecheck`, private), on branch `dev`
- [x] CLAUDE.md added
- [ ] 1. `feed-service/` — FastAPI app, `/health`, `/metrics` (3 metrics), JSON file logging
- [ ] 2. `docker-compose.yml` — 5 services, bridge network, named volumes
- [ ] 3. `prometheus/prometheus.yml` — static 5s scrape job
- [ ] 4. `grafana/` — provisioned 3-panel dashboard JSON
- [ ] 5. `log_watcher/` — polling tail + regex rules → incidents table
- [ ] 6. `watchdog/` — Docker SDK + `/health` poll, consecutive-breach, restart, SQLite, Discord
- [ ] 7. `scripts/induce_failure.sh` — kill / CPU spike / `tc` latency, each commented
- [ ] 8. `.github/workflows/ci.yml` — lint → pytest → Trivy → build → push
- [ ] 9. `README.md` — architecture, setup, demo walkthrough, AWS steps
- [ ] Local induced-failure demo run successfully end-to-end at least once
- [ ] 10a. EC2 launched + prepared (Docker installed)
- [ ] 10b. Security group scoped
- [ ] 10c. IAM role attached (least-privilege S3)
- [ ] 10d. S3 incident backup in watchdog
- [ ] 10e. Stack running on EC2, demo re-verified remotely
- [ ] 10f. *(optional)* CI-triggered SSH deploy

## Session log

Keep short, dated notes here so context survives between sessions.

- **2026-09-10** — Repo scaffolded (git init, GitHub private repo, `dev` branch). CLAUDE.md written from project spec. No components built yet.
