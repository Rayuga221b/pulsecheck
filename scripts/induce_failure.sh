#!/usr/bin/env bash
#
# induce_failure.sh — deliberately break the feed-service so the recovery
# pipeline (watchdog + log-watcher + Grafana) can be watched doing its job.
#
# Three scenarios, three *different* failure shapes, each caught by a different
# signal:
#
#   kill      hard crash        -> container leaves 'running'         -> watchdog CONTAINER probe
#   cpu       CPU-bound runaway  -> /health slows, ingestion lag rises -> Grafana; watchdog only if it
#                                                                        crosses the HTTP timeout
#   netlat    network latency    -> /health round-trip > timeout      -> watchdog HEALTH probe (indirectly:
#                                                                        it times out, it doesn't get a signal)
#
# Usage:
#   scripts/induce_failure.sh kill
#   scripts/induce_failure.sh cpu    [SECONDS]           (default 40)
#   scripts/induce_failure.sh netlat [DELAY_MS] [SECONDS] (default 600ms, 40s)
#   scripts/induce_failure.sh clear                      (remove any leftover tc netem)
#
# Run the watchdog and (optionally) the log-watcher in other terminals first:
#   python -m watchdog
#   python -m log_watcher
#
set -euo pipefail

# --- config -----------------------------------------------------------------
# Pull the container name and the watchdog's timing from .env if it's there, so
# this script's "expect detection in ~Ns" math matches the running watchdog.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -f "$REPO_ROOT/.env" ]]; then
  # shellcheck disable=SC1091
  set -a; . "$REPO_ROOT/.env"; set +a
fi
CONTAINER="${WATCHDOG_CONTAINER:-pulsecheck-feed-service}"
POLL="${WATCHDOG_POLL_SECONDS:-2.0}"
THRESHOLD="${BREACH_THRESHOLD:-3}"
HEALTH_TIMEOUT="${WATCHDOG_HEALTH_TIMEOUT_SECONDS:-3.0}"

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }

require_running() {
  if ! docker ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
    echo "ERROR: container '$CONTAINER' is not running. Start the stack first:" >&2
    echo "  docker-compose up -d" >&2
    exit 1
  fi
}

# =========================================================================
# Scenario 1: hard crash
# =========================================================================
scenario_kill() {
  require_running
  say "SCENARIO: kill — hard crash of $CONTAINER"
  note "Simulates: the process dying with no cleanup (segfault, OOM-killer, 'docker kill')."
  note "SIGKILL cannot be trapped, so PID 1 exits 137 (128 + 9) and the container leaves 'running'."
  note "Should be caught by: the watchdog's CONTAINER probe (state != 'running'),"
  note "  confirmed after ${THRESHOLD} polls (~$(awk "BEGIN{print $POLL*$THRESHOLD}")s), then 'docker restart'."
  note "Self-heals: yes — the watchdog restarts it and stamps resolved_at."
  say "-> docker kill $CONTAINER"
  docker kill "$CONTAINER"
  note "done. Watch: 'docker ps' (feed-service gone, then back), the watchdog log, incidents.db."
}

# =========================================================================
# Scenario 2: CPU-bound runaway
# =========================================================================
scenario_cpu() {
  require_running
  local secs="${1:-40}"
  say "SCENARIO: cpu — saturate every core inside $CONTAINER for ${secs}s"
  note "Simulates: a runaway loop / pathological query pegging the CPU."
  note "Primary symptom is *degradation*, not a crash: the uvicorn process gets starved of CPU,"
  note "  so /health and the write path slow down. Watch Grafana — request-latency p95 spikes and"
  note "  ingestion_lag_seconds climbs. The watchdog only restarts if a /health poll actually"
  note "  exceeds its ${HEALTH_TIMEOUT}s timeout for ${THRESHOLD} polls in a row; a milder spike is"
  note "  caught by humans looking at the dashboard, which is the honest outcome to show."

  local ncpu; ncpu="$(docker exec "$CONTAINER" sh -c 'nproc' 2>/dev/null || echo 2)"
  if docker exec "$CONTAINER" sh -c 'command -v stress' >/dev/null 2>&1; then
    # Canonical form — `stress` is in the image (added to the Dockerfile in M5).
    say "-> docker exec $CONTAINER stress --cpu $ncpu --timeout ${secs}s"
    docker exec "$CONTAINER" stress --cpu "$ncpu" --timeout "${secs}s"
  else
    # Fallback for environments where the image build couldn't apt-get `stress`
    # (this dev box has no egress to deb.debian.org). Same effect: one busy
    # Python loop per core (x2 for guaranteed contention with uvicorn).
    local workers=$(( ncpu * 2 ))
    say "-> stress not in image; fallback: $workers python busy-loops for ${secs}s"
    docker exec "$CONTAINER" python -c "
import multiprocessing as mp, time, sys
def spin(deadline):
    while time.time() < deadline:
        pass
end = time.time() + ${secs}
ps = [mp.Process(target=spin, args=(end,)) for _ in range(${workers})]
[p.start() for p in ps]
[p.join() for p in ps]
print('cpu load finished', file=sys.stderr)
"
  fi
  note "done. If the watchdog restarted it, incidents.db has a 'health' row; otherwise check Grafana."
}

# =========================================================================
# Scenario 3: network latency
# =========================================================================
# The feed-service container is deliberately NOT granted NET_ADMIN, so `tc` can't
# run *inside* it. Instead we add the delay on the host side of the container's
# veth pair — which is also the more realistic "the network to this box got slow"
# story. On a native Docker host that's `sudo tc ...` directly; with Colima the
# host netns lives in the Lima VM, reached via `colima ssh -- sudo`.
netns_host() {
  if command -v colima >/dev/null 2>&1; then
    colima ssh -- sudo "$@"
  else
    sudo "$@"
  fi
}

# Resolve the veth interface (host side) that is paired with the container's eth0.
# eth0's /sys/class/net/eth0/iflink is the peer's ifindex in the host netns.
resolve_veth() {
  local peer_ifindex
  peer_ifindex="$(docker exec "$CONTAINER" cat /sys/class/net/eth0/iflink | tr -d '[:space:]')"
  netns_host sh -c '
    for d in /sys/class/net/veth*; do
      [ -e "$d/ifindex" ] || continue
      if [ "$(cat "$d/ifindex")" = "'"$peer_ifindex"'" ]; then
        basename "$d"; exit 0
      fi
    done
    exit 1
  '
}

clear_netem() {
  local veth="${1:-}"
  [[ -z "$veth" ]] && veth="$(resolve_veth 2>/dev/null || true)"
  if [[ -n "$veth" ]]; then
    netns_host tc qdisc del dev "$veth" root 2>/dev/null || true
    note "cleared netem on $veth (if any)"
  fi
}

scenario_netlat() {
  require_running
  local delay_ms="${1:-600}" secs="${2:-40}"
  say "SCENARIO: netlat — add ${delay_ms}ms latency to ${CONTAINER}'s NIC for ${secs}s"
  note "Simulates: a slow network path to a dependency / the host (congestion, a bad link)."
  note "At the TCP level: every segment (SYN, the request, ACKs, the response) now takes an extra"
  note "  ${delay_ms}ms each way, so a multi-round-trip /health call inflates well past the raw delay."
  note "The watchdog has no 'slow' signal — it just sees the poll exceed its ${HEALTH_TIMEOUT}s timeout,"
  note "  which counts as a failed check. ${THRESHOLD} in a row -> restart (detected_via=health)."
  note "Self-heals: the restart recreates the veth, so the netem qdisc disappears with it;"
  note "  this script also deletes the qdisc on exit as a belt-and-braces."

  local veth; veth="$(resolve_veth)" || { echo "ERROR: could not find the container's veth" >&2; exit 1; }
  note "container eth0 is paired with host veth: $veth"
  # Make sure a stale qdisc from a previous aborted run doesn't make `add` fail.
  netns_host tc qdisc del dev "$veth" root 2>/dev/null || true

  # Clean up no matter how we exit (Ctrl-C, error, normal).
  trap 'clear_netem "$veth"' EXIT

  say "-> tc qdisc add dev $veth root netem delay ${delay_ms}ms"
  netns_host tc qdisc add dev "$veth" root netem delay "${delay_ms}ms"
  netns_host tc qdisc show dev "$veth"

  note "latency active for ${secs}s. Watch the watchdog log: breach 1/${THRESHOLD}, 2/${THRESHOLD}, CONFIRMED via health."
  sleep "$secs"

  # If the watchdog already restarted the container, $veth no longer exists and
  # this is a harmless no-op; otherwise it ends the induced latency.
  clear_netem "$veth"
  trap - EXIT
  note "done."
}

# --- dispatch -------------------------------------------------------------
case "${1:-}" in
  kill)    scenario_kill ;;
  cpu)     shift; scenario_cpu "${1:-}" ;;
  netlat)  shift; scenario_netlat "${1:-}" "${2:-}" ;;
  clear)   clear_netem ;;
  *)
    # Print the top-of-file comment block (up to the first non-comment line).
    sed -E '1d; /^[^#]/q; s/^# ?//' "$0"
    exit 1
    ;;
esac
