"""Entry point: ``python -m watchdog`` (run from the repo root, on the host).

    python -m watchdog
        Poll the feed-service container's Docker state and its ``/health``
        endpoint every ``WATCHDOG_POLL_SECONDS``. After ``BREACH_THRESHOLD``
        consecutive failed polls, treat the failure as confirmed:
          1. restart the container,
          2. write an open row to ``incidents.db``,
          3. POST a Slack alert (symptom / action / duration).
        On the next healthy poll, stamp ``resolved_at`` and send a recovery alert.

    python -m watchdog --once
        Run a single poll, print the verdict, exit. For eyeballing / scripts.

    python -m watchdog --dry-run
        Poll and run the full state machine, but do NOT restart, write rows or
        POST to Slack — just log what it *would* do. Useful to watch the breach
        counter climb during ``scripts/induce_failure.sh`` without side effects.

Runs on the **host**, not in a container: it needs the Docker socket and it
polls ``localhost:8000`` (feed-service publishes 8000 to the host).

Config — all from the environment / ``.env``, no constants in code:

    WATCHDOG_POLL_SECONDS         seconds between polls            (default 2.0)
    WATCHDOG_HEALTH_URL           the /health URL to poll          (default http://localhost:8000/health)
    WATCHDOG_HEALTH_TIMEOUT_SECONDS  per-poll HTTP timeout         (default 3.0)
    WATCHDOG_CONTAINER            container name to watch/restart  (default pulsecheck-feed-service)
    WATCHDOG_RESTART_TIMEOUT_SECONDS  SIGTERM->SIGKILL grace       (default 10)
    WATCHDOG_RECOVERY_CONFIRMATIONS   good polls needed to resolve (default 1)
    BREACH_THRESHOLD             consecutive fails before acting   (default 3)
    INCIDENTS_DB_PATH           shared SQLite incident log         (default incidents.db)
    SLACK_WEBHOOK_URL          incoming-webhook URL (secret)       (unset => alerts skipped)
    DOCKER_HOST               honoured by docker-py if set (Colima); unset uses the default socket
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time

# Make `import incidents` (the shared schema module at the repo root) work
# whether this is launched as `python -m watchdog` or by path.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import incidents  # noqa: E402  (deliberate: after the sys.path fix-up)

from watchdog.breach import BreachMonitor, Outcome  # noqa: E402
from watchdog.notify import SlackNotifier  # noqa: E402
from watchdog.probes import (  # noqa: E402
    ContainerProbe,
    HealthProbe,
    Prober,
    Verdict,
)

log = logging.getLogger("watchdog")

COMPONENT = "feed-service"  # what goes in the incident row's `component` column


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


def _build_prober() -> Prober:
    health = HealthProbe(
        url=_env("WATCHDOG_HEALTH_URL", "http://localhost:8000/health"),
        timeout_seconds=float(_env("WATCHDOG_HEALTH_TIMEOUT_SECONDS", "3.0")),
    )
    container = ContainerProbe(_env("WATCHDOG_CONTAINER", "pulsecheck-feed-service"))
    return Prober(health, container)


def _handle_confirmed_failure(
    verdict: Verdict,
    *,
    container: ContainerProbe,
    notifier: SlackNotifier,
    db_path: str,
    restart_timeout: int,
    dry_run: bool,
) -> None:
    """One confirmed failure: open the incident row, restart, record, alert.

    The incident row is opened *before* the restart so that if the restart itself
    throws, the failure is still on record. ``action_taken`` is filled in only
    after we know what actually happened.
    """
    detected_at = incidents.utcnow_iso()
    log.error(
        "failure CONFIRMED via %s: %s", verdict.detected_via, verdict.symptom
    )

    if dry_run:
        log.info("[dry-run] would open incident, restart %s, and send Slack alert",
                 container.name)
        return

    incident_id = incidents.open_incident(
        component=COMPONENT,
        detected_via=verdict.detected_via,
        symptom=verdict.symptom,
        detected_at=detected_at,
    )

    # Restart the container. Even when the root cause is a downed dependency
    # (container "running" + /health 503) rather than a dead process, we still
    # restart: it is cheap, it clears any half-open PG/Redis connections, and if
    # the dependency has recovered it gets writes flowing again without waiting
    # on the app's own backoff. If it genuinely can't help, the *alert* is the
    # point — a human now knows to look at Postgres/Redis.
    action_taken: str
    try:
        container.restart(timeout_seconds=restart_timeout)
        action_taken = f"restarted container {container.name}"
        log.info("restarted container %s", container.name)
    except Exception as exc:
        action_taken = f"restart FAILED: {exc}"
        log.exception("failed to restart container")

    incidents.set_action(incident_id, action_taken, db_path=db_path)

    if notifier.enabled:
        sent = notifier.incident(
            component=COMPONENT,
            symptom=verdict.symptom,
            detected_via=verdict.detected_via,
            action_taken=action_taken,
            detected_at=detected_at,
            incident_id=incident_id,
        )
        log.info("slack incident alert %s", "sent" if sent else "FAILED")
    else:
        log.warning("SLACK_WEBHOOK_URL unset — incident #%s not sent to Slack", incident_id)


def _handle_recovery(
    downtime_seconds: int | None,
    *,
    notifier: SlackNotifier,
    db_path: str,
    dry_run: bool,
) -> None:
    """Target is healthy again: close the most recent open incident, alert."""
    resolved_at = incidents.utcnow_iso()
    log.info("RECOVERED after ~%ss", downtime_seconds)

    if dry_run:
        log.info("[dry-run] would resolve the open incident and send Slack recovery alert")
        return

    incident_id = _resolve_latest_open(db_path, resolved_at)
    if incident_id is None:
        log.warning("recovery with no open incident row to close (already resolved?)")
        return

    if notifier.enabled:
        sent = notifier.recovery(
            component=COMPONENT,
            downtime_seconds=downtime_seconds,
            resolved_at=resolved_at,
            incident_id=incident_id,
        )
        log.info("slack recovery alert %s", "sent" if sent else "FAILED")


def _resolve_latest_open(db_path: str, resolved_at: str) -> int | None:
    """Find the newest still-open incident for this component and close it.

    The state machine guarantees at most one incident is open for the target at a
    time, so "newest open row" is unambiguous. Done here (not in ``incidents.py``)
    because it is watchdog policy, not schema; the actual UPDATE is
    ``incidents.resolve_incident``.
    """
    conn = incidents.connect(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM incidents "
            "WHERE component = ? AND resolved_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (COMPONENT,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    incidents.resolve_incident(int(row["id"]), resolved_at=resolved_at, db_path=db_path)
    return int(row["id"])


def _run_once(prober: Prober) -> int:
    verdict = prober.poll()
    if verdict.ok:
        log.info(
            "OK — container=%s health=%s",
            verdict.container_state, verdict.health_status,
        )
        return 0
    log.error("FAIL via %s — %s", verdict.detected_via, verdict.symptom)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="watchdog", description=__doc__)
    parser.add_argument("--once", action="store_true", help="single poll, print, exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the state machine but take no action (no restart / DB / Slack)",
    )
    args = parser.parse_args(argv)

    _setup_logging()

    poll_seconds = float(_env("WATCHDOG_POLL_SECONDS", "2.0"))
    breach_threshold = int(_env("BREACH_THRESHOLD", "3"))
    recovery_threshold = int(_env("WATCHDOG_RECOVERY_CONFIRMATIONS", "1"))
    restart_timeout = int(_env("WATCHDOG_RESTART_TIMEOUT_SECONDS", "10"))
    db_path = _env("INCIDENTS_DB_PATH", "incidents.db")

    try:
        prober = _build_prober()
    except Exception as exc:
        # Almost always a bad/absent Docker socket. Fail loudly at startup.
        log.error("cannot start watchdog: %s", exc)
        return 2

    if args.once:
        return _run_once(prober)

    incidents.init(db_path)
    notifier = SlackNotifier(os.getenv("SLACK_WEBHOOK_URL") or None)
    monitor = BreachMonitor(
        breach_threshold=breach_threshold,
        recovery_threshold=recovery_threshold,
    )
    container = prober.container  # same object, reused to issue the restart

    log.info(
        "watchdog up: poll=%ss threshold=%s recovery=%s container=%s db=%s slack=%s%s",
        poll_seconds, breach_threshold, recovery_threshold,
        _env("WATCHDOG_CONTAINER", "pulsecheck-feed-service"), db_path,
        "on" if notifier.enabled else "off",
        " [DRY-RUN]" if args.dry_run else "",
    )

    while True:
        verdict = prober.poll()
        decision = monitor.observe(verdict, now=time.time())

        if decision.outcome is Outcome.NOTHING:
            log.debug("ok (streak reset); container=%s", verdict.container_state)
        elif decision.outcome is Outcome.STILL_BREACHING:
            # Under threshold: record the near-miss, do nothing. This is the
            # anti-flap window — a blip gets these lines and then recovers.
            log.warning(
                "breach %d/%d via %s: %s",
                decision.consecutive_failures, breach_threshold,
                verdict.detected_via, verdict.symptom,
            )
        elif decision.outcome is Outcome.FAILURE_CONFIRMED:
            _handle_confirmed_failure(
                verdict,
                container=container,
                notifier=notifier,
                db_path=db_path,
                restart_timeout=restart_timeout,
                dry_run=args.dry_run,
            )
        elif decision.outcome is Outcome.RECOVERED:
            _handle_recovery(
                decision.downtime_seconds,
                notifier=notifier,
                db_path=db_path,
                dry_run=args.dry_run,
            )

        time.sleep(poll_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        # Ctrl-C is a normal way to stop a host daemon; don't dump a traceback.
        sys.exit(130)
