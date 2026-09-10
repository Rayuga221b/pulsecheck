"""The consecutive-breach state machine — the core of the watchdog.

This is the most important file in the project, so it is the most heavily
commented. Everything here is pure in-memory logic: it takes a stream of
per-poll verdicts and decides *when* to act. It never talks to Docker, HTTP,
SQLite or Slack — that is the caller's job (``watchdog/__main__.py``). Keeping it
pure is what makes it unit-testable with fakes and no real containers (M6).

--------------------------------------------------------------------------------
Why "N consecutive failures" instead of "act on the first failed check"
--------------------------------------------------------------------------------
A single failed probe is a weak signal. Any of these produce one bad check and
then fix themselves with no help from us:

* the container is mid-restart from an unrelated cause (Compose recreate, a
  `docker cp`, an OOM-killed sidecar) and is back within a second;
* the app had a GC / event-loop pause longer than the probe timeout;
* a one-off TCP hiccup on the loopback poll;
* Prometheus and the watchdog happened to hit ``/health`` at the same instant a
  connection-pool slot was being recycled.

If we restarted the feed-service container on any of those, the *cure* (a full
container restart: dropped in-flight writes, cold connection pools, ~seconds of
missing ticks) would be worse and more frequent than the disease. Restarting is a
big hammer; we only swing it once we are sure the fault is real and persistent.

"Sure" here is defined operationally: ``breach_threshold`` consecutive failed
checks, with no successful check in between. One good check resets the counter to
zero — a fault that isn't continuous isn't the kind this hammer fixes.

--------------------------------------------------------------------------------
The cost of the threshold: detection latency
--------------------------------------------------------------------------------
The trade-off is latency. With ``poll_interval`` = 2s and ``breach_threshold`` =
3, a hard down takes ~3 polls ≈ 4–6s to *confirm* (the first failure can land
anywhere in a poll interval). That is an acceptable price here: this is an L1
auto-recovery aid, not a millisecond-SLA system, and 6s of certainty beats 2s of
guessing. Both numbers live in ``.env`` (``BREACH_THRESHOLD``,
``WATCHDOG_POLL_SECONDS``) with this reasoning, so neither is a magic constant.

--------------------------------------------------------------------------------
Recovery is deliberately *not* debounced the same way
--------------------------------------------------------------------------------
We require N *failures* to open an incident, but only ``recovery_threshold``
(default 1) *successes* to close it. The asymmetry is intentional:

* A false "it's down" is expensive — it triggers a restart. Hence N.
* A false "it's back" just closes the incident row a little early; the very next
  failed poll re-opens a new one. Cheap to be wrong. Hence 1.

``recovery_threshold`` is still configurable for anyone who wants a steadier
"resolved" signal.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Optional

from watchdog.probes import Verdict


class Outcome(enum.Enum):
    """What the caller should do as a result of the verdict it just fed in.

    The state machine returns exactly one of these per ``observe()`` call. Only
    two of them are actionable; the rest tell the loop to keep polling.
    """

    NOTHING = "nothing"                 # steady state (healthy, or already-open incident still down)
    FAILURE_CONFIRMED = "failure_confirmed"  # threshold just reached -> open incident, restart, alert
    RECOVERED = "recovered"            # target came back -> stamp resolved_at, send recovery alert
    STILL_BREACHING = "still_breaching"  # failing, but under threshold -> log only, do not act yet


@dataclass
class Decision:
    """The state machine's answer for one poll: an ``Outcome`` plus context the
    caller needs to write the incident row / Slack message."""

    outcome: Outcome
    # Number of consecutive failed checks at this point (0 when healthy).
    consecutive_failures: int
    # The verdict that drove this decision — carries the symptom text and which
    # signal (``health`` / ``container``) failed, for the incident row.
    verdict: Verdict
    # Only set on RECOVERED: how long the incident was open, in whole seconds.
    downtime_seconds: Optional[int] = None


@dataclass
class BreachMonitor:
    """Tracks one monitored target (here: the feed-service container).

    Usage from the poll loop::

        monitor = BreachMonitor(breach_threshold=3)
        while True:
            verdict = probe()                 # watchdog.probes
            decision = monitor.observe(verdict, now=time.time())
            if decision.outcome is Outcome.FAILURE_CONFIRMED:
                ... restart + open incident + Slack ...
            elif decision.outcome is Outcome.RECOVERED:
                ... resolve incident + Slack ...
            sleep(poll_interval)
    """

    breach_threshold: int = 3
    recovery_threshold: int = 1

    # ---- mutable state (not constructor args) ----
    _consecutive_failures: int = field(default=0, init=False)
    _consecutive_successes: int = field(default=0, init=False)
    # True once a failure has been confirmed and an incident opened, until the
    # matching recovery. While this is True we do NOT keep firing FAILURE_CONFIRMED
    # every poll — one incident, one restart, one alert, until it recovers.
    _incident_open: bool = field(default=False, init=False)
    # Wall-clock (epoch seconds) of the poll that confirmed the failure. Used to
    # report downtime on recovery.
    _incident_started_at: Optional[float] = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.breach_threshold < 1:
            raise ValueError("breach_threshold must be >= 1")
        if self.recovery_threshold < 1:
            raise ValueError("recovery_threshold must be >= 1")

    # Small read-only views, handy for logging and tests.
    @property
    def incident_open(self) -> bool:
        return self._incident_open

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def observe(self, verdict: Verdict, *, now: float) -> Decision:
        """Feed one poll result in; get back what to do.

        ``now`` is epoch seconds (``time.time()``). It is a parameter, not read
        from the clock in here, so tests can drive time by hand.
        """
        if verdict.ok:
            return self._on_success(verdict, now)
        return self._on_failure(verdict, now)

    # ------------------------------------------------------------------ helpers

    def _on_failure(self, verdict: Verdict, now: float) -> Decision:
        # A failure breaks any recovery streak in progress.
        self._consecutive_successes = 0
        self._consecutive_failures += 1

        if self._incident_open:
            # We already know it's down and have already acted. Don't restart
            # again on every poll — that would be a restart loop against a fault
            # the restart can't fix (e.g. Postgres itself is down). Just wait for
            # recovery. A future refinement could re-attempt after a long grace
            # period; for L1 scope, one action per incident is the safe default.
            return Decision(Outcome.NOTHING, self._consecutive_failures, verdict)

        if self._consecutive_failures < self.breach_threshold:
            # Failing, but not yet sure. This is the window where a blip gets a
            # chance to resolve itself before we reach for the hammer.
            return Decision(
                Outcome.STILL_BREACHING, self._consecutive_failures, verdict
            )

        # Exactly at the threshold, and no incident open yet: this is the moment
        # the failure becomes *confirmed*. Latch the incident so we act once.
        self._incident_open = True
        self._incident_started_at = now
        return Decision(
            Outcome.FAILURE_CONFIRMED, self._consecutive_failures, verdict
        )

    def _on_success(self, verdict: Verdict, now: float) -> Decision:
        # A success breaks any failure streak immediately — that is the whole
        # point of "*consecutive*": an intermittent fault never accumulates.
        self._consecutive_failures = 0
        self._consecutive_successes += 1

        if not self._incident_open:
            # Healthy and staying healthy. Also covers the happy case where a
            # blip (1 or 2 failures, below threshold) just recovered on its own —
            # exactly the outcome the threshold exists to wait for. Nothing to do.
            return Decision(Outcome.NOTHING, 0, verdict)

        # An incident is open and we now have a good check. Close it once we've
        # seen enough good checks in a row (default: 1).
        if self._consecutive_successes < self.recovery_threshold:
            return Decision(Outcome.NOTHING, 0, verdict)

        downtime = None
        if self._incident_started_at is not None:
            downtime = max(0, round(now - self._incident_started_at))

        # Reset for the next incident.
        self._incident_open = False
        self._incident_started_at = None
        self._consecutive_successes = 0

        return Decision(Outcome.RECOVERED, 0, verdict, downtime_seconds=downtime)
