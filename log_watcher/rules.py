"""Detection rules — the "memory" in "grep with memory".

Each rule is fed one parsed log record at a time and returns a ``Detection`` the
moment it decides something is wrong, or ``None``. State (the rolling window, the
cooldown) lives inside the rule instance.

A parsed record is a plain dict:
    {"ts": datetime, "level": "ERROR", "event": "tick_write_error",
     "detail": "...", "raw": "<original line>"}

Two rule shapes cover everything M3 needs:

* ``RollingCountRule`` — "N matching lines within W seconds" (error bursts,
  repeated connection failures). This is the windowed-counter pattern; it is the
  same idea as the watchdog's consecutive-breach logic, just time-based instead
  of check-based.
* ``KnownStringRule`` — "this specific line always means trouble, regardless of
  rate" (the app itself logging that it gave up).

Every rule has a **cooldown**: after it fires it stays quiet for
``cooldown_seconds`` of log time. Without it, a 30-second outage at ~16 writes/s
would punch hundreds of near-identical rows into the incidents table. One
sustained fault should be one incident row.

All timing is in **log event-time** (the record's ``ts``), not wall-clock. That
makes ``--replay`` of a captured log fully deterministic and lets a replayed gap
correctly end a cooldown.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Deque, Iterable, Optional

# What every Detection from this module carries into the incidents table.
DETECTED_VIA = "log-watcher"
COMPONENT = "feed-service"  # the only service that writes this log


@dataclass(frozen=True)
class Detection:
    rule: str          # which rule fired, e.g. "error_burst"
    component: str      # "feed-service"
    detected_via: str   # "log-watcher"
    detected_at: str    # ISO-8601 UTC string, taken from the triggering record
    symptom: str        # human-readable: what was observed


def _iso_z(ts: datetime) -> str:
    return ts.isoformat(timespec="seconds").replace("+00:00", "Z")


class RollingCountRule:
    """Fire when ``threshold`` matching records fall inside a ``window_seconds``
    sliding window (by event-time), then cool down."""

    def __init__(
        self,
        name: str,
        *,
        match: Callable[[dict], bool],
        threshold: int,
        window_seconds: float,
        cooldown_seconds: float,
        symptom: str,
    ) -> None:
        self.name = name
        self._match = match
        self._threshold = threshold
        self._window = timedelta(seconds=window_seconds)
        self._cooldown = timedelta(seconds=cooldown_seconds)
        self._symptom_template = symptom
        self._hits: Deque[datetime] = deque()
        self._quiet_until: Optional[datetime] = None

    def feed(self, record: dict) -> Optional[Detection]:
        if not self._match(record):
            return None

        now = record["ts"]
        self._hits.append(now)
        # Drop anything that has aged out of the window.
        cutoff = now - self._window
        while self._hits and self._hits[0] < cutoff:
            self._hits.popleft()

        if self._quiet_until is not None and now < self._quiet_until:
            return None  # still cooling down from the last fire
        if len(self._hits) < self._threshold:
            return None

        self._quiet_until = now + self._cooldown
        count = len(self._hits)
        self._hits.clear()  # this burst is now "used"; start counting the next one fresh
        symptom = self._symptom_template.format(
            count=count,
            window=int(self._window.total_seconds()),
            detail=record.get("detail", ""),
            event=record.get("event", ""),
        )
        return Detection(self.name, COMPONENT, DETECTED_VIA, _iso_z(now), symptom)


class KnownStringRule:
    """Fire on any record whose event tag or raw text contains one of the known
    failure markers. Has its own cooldown so a marker that repeats every loop is
    still just one incident."""

    def __init__(
        self,
        name: str,
        *,
        markers: Iterable[str],
        cooldown_seconds: float,
        symptom: str,
    ) -> None:
        self.name = name
        self._markers = [m for m in markers if m]
        self._cooldown = timedelta(seconds=cooldown_seconds)
        self._symptom_template = symptom
        self._quiet_until: Optional[datetime] = None

    def feed(self, record: dict) -> Optional[Detection]:
        hay = f"{record.get('event', '')} {record.get('raw', '')}"
        hit = next((m for m in self._markers if m in hay), None)
        if hit is None:
            return None

        now = record["ts"]
        if self._quiet_until is not None and now < self._quiet_until:
            return None
        self._quiet_until = now + self._cooldown
        symptom = self._symptom_template.format(
            marker=hit, detail=record.get("detail", ""), event=record.get("event", "")
        )
        return Detection(self.name, COMPONENT, DETECTED_VIA, _iso_z(now), symptom)


def default_rules(
    *,
    error_burst_count: int,
    error_burst_window_s: float,
    conn_fail_count: int,
    conn_fail_window_s: float,
    cooldown_s: float,
    known_markers: Iterable[str],
) -> list:
    """The three rules from the spec, wired with justified thresholds.

    * **Error burst** — feed-service runs ~16 write attempts/second, so a genuine
      outage produces ERROR lines almost immediately and in volume. Requiring
      ``error_burst_count`` (default 5) within ``error_burst_window_s`` (default
      20s) fires within a second of a real fault but ignores a lone
      ``tick_write_error`` that the app's own backoff recovers from.

    * **Repeated connection failures** — counts the app's own
      ``dependency_retry`` lines. Its backoff is 1s, 2s, 4s, 8s; seeing
      ``conn_fail_count`` (default 3) of them inside ``conn_fail_window_s``
      (default 60s) means a dependency has been unreachable for ~7s+ and is not
      snapping straight back. Same "3 strikes" idea as ``BREACH_THRESHOLD``.

    * **Known failure strings** — events where the service has *already concluded*
      it is broken: ``dependency_unreachable`` (backoff exhausted, connect gave
      up) and ``generator_reconnect_failed``. These are incidents at any rate.
    """
    return [
        RollingCountRule(
            "error_burst",
            match=lambda r: r.get("level") == "ERROR",
            threshold=error_burst_count,
            window_seconds=error_burst_window_s,
            cooldown_seconds=cooldown_s,
            symptom="error burst: {count} ERROR log lines within {window}s "
            "(latest: {event} — {detail})",
        ),
        RollingCountRule(
            "connection_failures",
            match=lambda r: r.get("event") == "dependency_retry",
            threshold=conn_fail_count,
            window_seconds=conn_fail_window_s,
            cooldown_seconds=cooldown_s,
            symptom="repeated dependency connection failures: {count} retry "
            "attempts within {window}s (latest: {detail})",
        ),
        KnownStringRule(
            "known_failure",
            markers=known_markers,
            cooldown_seconds=cooldown_s,
            symptom="known failure marker '{marker}' seen ({detail})",
        ),
    ]
