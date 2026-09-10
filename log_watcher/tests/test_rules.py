"""Unit tests for the log-watcher detection rules (``log_watcher/rules.py``).

The rules are pure, stdlib-only, and all their timing is in *log event-time*
(the record's ``ts``), never wall-clock — which is exactly what makes them
testable without sleeping: every test hands the rule a sequence of records with
hand-picked timestamps.

Coverage:

* ``RollingCountRule`` fires on the Nth match inside the window, not the (N-1)th;
* matches that fall outside the sliding window don't accumulate;
* the per-rule cooldown collapses one sustained outage into one detection;
* ``KnownStringRule`` fires on a marker in the event tag or the raw line;
* the three wired ``default_rules`` produce exactly one row per rule when
  replayed over the committed ``sample_incident.log`` fixture.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

from log_watcher.rules import KnownStringRule, RollingCountRule, default_rules

_BASE = datetime(2026, 9, 10, 12, 0, 0, tzinfo=timezone.utc)
_SAMPLE_LOG = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sample_incident.log")


def rec(offset_s: float, level: str = "INFO", event: str = "tick_write", detail: str = "") -> dict:
    """One parsed log record ``offset_s`` seconds after the fixed base time."""
    return {
        "ts": _BASE + timedelta(seconds=offset_s),
        "level": level,
        "event": event,
        "detail": detail,
        "raw": json.dumps({"level": level, "event": event, "detail": detail}),
    }


# ---------------------------------------------------------------- RollingCountRule


def _error_burst_rule() -> RollingCountRule:
    return RollingCountRule(
        "error_burst",
        match=lambda r: r.get("level") == "ERROR",
        threshold=5,
        window_seconds=20,
        cooldown_seconds=120,
        symptom="error burst: {count} in {window}s (latest: {event} — {detail})",
    )


def test_rolling_count_fires_only_on_the_nth_match_in_window() -> None:
    rule = _error_burst_rule()

    # Four ERRORs, one second apart — under the threshold of 5.
    results = [rule.feed(rec(i, level="ERROR")) for i in range(4)]
    assert results == [None, None, None, None]

    # The fifth, still inside the 20s window, trips it.
    hit = rule.feed(rec(4, level="ERROR", event="tick_write_error", detail="redis refused"))
    assert hit is not None
    assert hit.rule == "error_burst"
    assert hit.component == "feed-service"
    assert hit.detected_via == "log-watcher"
    assert "5" in hit.symptom and "redis refused" in hit.symptom
    # detected_at is taken from the triggering record, not "now".
    assert hit.detected_at == "2026-09-10T12:00:04Z"


def test_non_matching_records_do_not_count() -> None:
    rule = _error_burst_rule()

    for i in range(10):
        # INFO lines: the match predicate ignores them entirely.
        assert rule.feed(rec(i, level="INFO")) is None


def test_matches_outside_the_sliding_window_do_not_accumulate() -> None:
    rule = _error_burst_rule()

    # Five ERRORs but spread 10s apart => the window (20s) never holds 5 at once.
    hits = [rule.feed(rec(i * 10, level="ERROR")) for i in range(5)]
    assert all(h is None for h in hits)


def test_cooldown_collapses_a_sustained_outage_into_one_detection() -> None:
    rule = _error_burst_rule()

    # First burst: 5 ERRORs in 5s -> one detection.
    first = [rule.feed(rec(i, level="ERROR")) for i in range(5)]
    assert sum(h is not None for h in first) == 1

    # Keep failing for the next 60s (well inside the 120s cooldown): silent.
    during_cooldown = [rule.feed(rec(10 + i, level="ERROR")) for i in range(30)]
    assert all(h is None for h in during_cooldown)

    # After the cooldown expires, a fresh burst is allowed to fire again.
    after = [rule.feed(rec(200 + i, level="ERROR")) for i in range(5)]
    assert sum(h is not None for h in after) == 1


# ---------------------------------------------------------------- KnownStringRule


def test_known_string_rule_fires_on_marker_and_then_cools_down() -> None:
    rule = KnownStringRule(
        "known_failure",
        markers=["dependency_unreachable", "generator_reconnect_failed"],
        cooldown_seconds=120,
        symptom="known failure marker '{marker}' seen ({detail})",
    )

    miss = rule.feed(rec(0, level="INFO", event="tick_write"))
    assert miss is None

    hit = rule.feed(rec(1, level="ERROR", event="dependency_unreachable", detail="gave up after 5"))
    assert hit is not None
    assert "dependency_unreachable" in hit.symptom
    assert "gave up after 5" in hit.symptom

    # Same marker again within the cooldown -> suppressed.
    assert rule.feed(rec(30, level="ERROR", event="dependency_unreachable")) is None
    # ...and allowed again once the cooldown has passed.
    assert rule.feed(rec(200, level="ERROR", event="generator_reconnect_failed")) is not None


# ---------------------------------------------------------------- default_rules / replay


def _parse_line(line: str) -> dict | None:
    """Minimal stand-in for log_watcher.__main__._parse_record for the fixture replay."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    ts = datetime.fromisoformat(obj["ts"])
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return {
        "ts": ts,
        "level": obj.get("level", ""),
        "event": obj.get("event", ""),
        "detail": obj.get("detail", ""),
        "raw": line,
    }


def test_sample_incident_log_yields_one_detection_per_rule() -> None:
    """The committed fixture is a single redis outage. All three rules should
    trip exactly once — this is the M3 checkpoint, frozen as a regression test."""
    rules = default_rules(
        error_burst_count=5,
        error_burst_window_s=20,
        conn_fail_count=3,
        conn_fail_window_s=60,
        cooldown_s=120,
        known_markers=["dependency_unreachable", "generator_reconnect_failed"],
    )

    fired: list[str] = []
    with open(_SAMPLE_LOG, encoding="utf-8") as fh:
        for line in fh:
            record = _parse_line(line.strip())
            if record is None:
                continue
            for rule in rules:
                detection = rule.feed(record)
                if detection is not None:
                    fired.append(detection.rule)

    assert sorted(fired) == ["connection_failures", "error_burst", "known_failure"]
