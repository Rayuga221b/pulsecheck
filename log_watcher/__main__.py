"""Entry point: ``python -m log_watcher`` (run from the repo root).

Live mode (default):
    python -m log_watcher
        Tail ``LOG_WATCHER_LOG_FILE`` from EOF, apply the rules forever, write an
        ``incidents`` row + a stdout line on every detection.

Replay mode (the M3 checkpoint):
    python -m log_watcher --replay path/to/captured.log
        Run the rules over a captured log from the top, print detections, exit.
        Add ``--dry-run`` to skip the DB write.

Runs on the **host**, not in a container: it watches the bind-mounted
``./logs/feed-service.log`` that the feed-service container writes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

# Allow `import incidents` (the shared schema module at the repo root) whether
# this is run as `python -m log_watcher` or `python log_watcher/__main__.py`.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import incidents  # noqa: E402  (deliberate: after the sys.path fix-up)

from log_watcher.rules import default_rules  # noqa: E402
from log_watcher.tailer import FileTailer  # noqa: E402


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _build_rules() -> list:
    return default_rules(
        error_burst_count=int(_env("LOG_WATCHER_ERROR_BURST_COUNT", "5")),
        error_burst_window_s=float(_env("LOG_WATCHER_ERROR_BURST_WINDOW_SECONDS", "20")),
        conn_fail_count=int(_env("LOG_WATCHER_CONN_FAIL_COUNT", "3")),
        conn_fail_window_s=float(_env("LOG_WATCHER_CONN_FAIL_WINDOW_SECONDS", "60")),
        cooldown_s=float(_env("LOG_WATCHER_COOLDOWN_SECONDS", "120")),
        known_markers=_env(
            "LOG_WATCHER_KNOWN_MARKERS",
            "dependency_unreachable,generator_reconnect_failed",
        ).split(","),
    )


def _parse_record(line: str) -> dict | None:
    """Turn one raw log line into the dict the rules expect, or None if it isn't
    a feed-service JSON line we can use."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        # Not our format (a stray stack-trace fragment, a blank line). The rules
        # only reason about structured records; skip it.
        return None
    if not isinstance(obj, dict):
        return None

    raw_ts = obj.get("ts")
    try:
        ts = datetime.fromisoformat(raw_ts) if raw_ts else datetime.now(timezone.utc)
    except (TypeError, ValueError):
        ts = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    return {
        "ts": ts,
        "level": obj.get("level", ""),
        "event": obj.get("event", ""),
        "detail": obj.get("detail", ""),
        "raw": line,
    }


def _handle(detection, *, db_path: str, dry_run: bool) -> None:
    incident_id = None
    if not dry_run:
        incident_id = incidents.open_incident(
            component=detection.component,
            detected_via=detection.detected_via,
            symptom=detection.symptom,
            detected_at=detection.detected_at,
            db_path=db_path,
        )
    tag = f"incident #{incident_id}" if incident_id is not None else "incident (dry-run)"
    print(
        f"[{detection.detected_at}] {tag} via {detection.rule}: {detection.symptom}",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="log_watcher", description=__doc__)
    parser.add_argument(
        "--replay",
        metavar="FILE",
        help="process FILE from the start once, then exit (checkpoint / testing)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print detections but do not write incident rows",
    )
    parser.add_argument(
        "--db",
        default=incidents.INCIDENTS_DB_PATH,
        help="incidents DB path (default: $INCIDENTS_DB_PATH or incidents.db)",
    )
    args = parser.parse_args(argv)

    rules = _build_rules()
    if not args.dry_run:
        incidents.init(args.db)

    if args.replay:
        source = FileTailer(args.replay, from_start=True).read_once()
        mode = f"replay {args.replay}"
    else:
        log_file = _env("LOG_WATCHER_LOG_FILE", "logs/feed-service.log")
        poll = float(_env("LOG_WATCHER_POLL_SECONDS", "1.0"))
        source = FileTailer(log_file, poll_seconds=poll).follow()
        mode = f"follow {log_file} (poll {poll}s)"

    print(f"log-watcher starting: {mode}; db={args.db}", flush=True)

    seen = 0
    for line in source:
        record = _parse_record(line)
        if record is None:
            continue
        seen += 1
        for rule in rules:
            detection = rule.feed(record)
            if detection is not None:
                _handle(detection, db_path=args.db, dry_run=args.dry_run)

    print(f"log-watcher done: {seen} records processed", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
