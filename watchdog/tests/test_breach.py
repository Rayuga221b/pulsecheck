"""Unit tests for the consecutive-breach state machine (``watchdog/breach.py``).

This is the core of the project, so it gets the most thorough tests. Everything
here runs against fakes: ``breach.py`` is pure in-memory logic, so there is no
Docker, no HTTP, no SQLite and no sleeping. Wall-clock time is passed in as the
``now=`` argument, so "downtime" is exercised without any real elapsed time.

What we are pinning down:

* a blip below the threshold never triggers an action, and clears itself;
* it takes exactly ``breach_threshold`` *consecutive* failures to confirm — any
  single success in between resets the count (the whole point of "consecutive");
* once confirmed, further failed polls do NOT re-fire (one incident, one
  restart) until a recovery has happened;
* recovery is asymmetric: only ``recovery_threshold`` successes (default 1) to
  close the incident, and it reports downtime;
* the constructor rejects nonsensical thresholds.
"""

from __future__ import annotations

import pytest

from watchdog.breach import BreachMonitor, Outcome
from watchdog.probes import Verdict

# ---------------------------------------------------------------- fake verdicts

OK = Verdict.healthy(container_state="running", health_status=200)


def fail(via: str = "health", symptom: str = "/health returned 503: redis: error") -> Verdict:
    """A failing verdict. ``via`` / ``symptom`` mirror what ``Prober.poll`` builds."""
    return Verdict(ok=False, detected_via=via, symptom=symptom)


def feed(monitor: BreachMonitor, verdicts, *, start: float = 1_000.0, step: float = 2.0):
    """Feed a sequence of verdicts one poll apart; return the list of Decisions."""
    decisions = []
    now = start
    for verdict in verdicts:
        decisions.append(monitor.observe(verdict, now=now))
        now += step
    return decisions


# ---------------------------------------------------------------- happy path


def test_healthy_polls_do_nothing() -> None:
    monitor = BreachMonitor(breach_threshold=3)

    outcomes = [d.outcome for d in feed(monitor, [OK, OK, OK, OK])]

    assert outcomes == [Outcome.NOTHING] * 4
    assert monitor.incident_open is False
    assert monitor.consecutive_failures == 0


# ---------------------------------------------------------------- sub-threshold blip


def test_blip_below_threshold_never_acts_and_self_clears() -> None:
    monitor = BreachMonitor(breach_threshold=3)

    # 2 failures (threshold is 3) then a good check.
    decisions = feed(monitor, [fail(), fail(), OK])

    assert [d.outcome for d in decisions] == [
        Outcome.STILL_BREACHING,  # 1/3
        Outcome.STILL_BREACHING,  # 2/3
        Outcome.NOTHING,          # recovered on its own before we reached for the hammer
    ]
    assert decisions[1].consecutive_failures == 2
    assert monitor.incident_open is False


def test_a_single_success_resets_the_consecutive_count() -> None:
    """fail, fail, OK, fail, fail — still must NOT confirm: the run was broken."""
    monitor = BreachMonitor(breach_threshold=3)

    decisions = feed(monitor, [fail(), fail(), OK, fail(), fail()])

    assert [d.outcome for d in decisions] == [
        Outcome.STILL_BREACHING,
        Outcome.STILL_BREACHING,
        Outcome.NOTHING,
        Outcome.STILL_BREACHING,  # counter restarted at 1
        Outcome.STILL_BREACHING,  # 2/3 — no confirmation
    ]
    assert monitor.incident_open is False


# ---------------------------------------------------------------- confirmation


def test_n_consecutive_failures_confirm_exactly_on_the_nth() -> None:
    monitor = BreachMonitor(breach_threshold=3)

    decisions = feed(monitor, [fail(), fail(), fail()])

    assert [d.outcome for d in decisions] == [
        Outcome.STILL_BREACHING,
        Outcome.STILL_BREACHING,
        Outcome.FAILURE_CONFIRMED,
    ]
    confirmed = decisions[-1]
    assert confirmed.consecutive_failures == 3
    assert confirmed.verdict.detected_via == "health"
    assert monitor.incident_open is True


def test_threshold_of_one_confirms_on_the_first_failure() -> None:
    monitor = BreachMonitor(breach_threshold=1)

    decisions = feed(monitor, [OK, fail(via="container", symptom="container state is 'exited'")])

    assert decisions[0].outcome is Outcome.NOTHING
    assert decisions[1].outcome is Outcome.FAILURE_CONFIRMED
    assert decisions[1].verdict.detected_via == "container"


def test_confirmed_failure_does_not_refire_on_later_failed_polls() -> None:
    """One incident, one restart. While it stays down we return NOTHING, not a
    second FAILURE_CONFIRMED — otherwise the watchdog would restart-loop against a
    fault a restart cannot fix (e.g. Postgres itself is down)."""
    monitor = BreachMonitor(breach_threshold=2)

    decisions = feed(monitor, [fail(), fail(), fail(), fail(), fail()])

    outcomes = [d.outcome for d in decisions]
    assert outcomes == [
        Outcome.STILL_BREACHING,
        Outcome.FAILURE_CONFIRMED,
        Outcome.NOTHING,
        Outcome.NOTHING,
        Outcome.NOTHING,
    ]
    # The failure counter still climbs (useful for logging) even while latched.
    assert decisions[-1].consecutive_failures == 5


# ---------------------------------------------------------------- recovery


def test_recovery_after_confirmation_reports_downtime() -> None:
    monitor = BreachMonitor(breach_threshold=2)

    # step=2s: fail@1000 (1/2), fail@1002 (confirmed, incident starts), OK@1004.
    decisions = feed(monitor, [fail(), fail(), OK])

    recovered = decisions[-1]
    assert recovered.outcome is Outcome.RECOVERED
    assert recovered.downtime_seconds == 2  # 1004 - 1002
    assert monitor.incident_open is False
    assert monitor.consecutive_failures == 0


def test_next_failure_after_recovery_opens_a_fresh_incident() -> None:
    monitor = BreachMonitor(breach_threshold=2)

    decisions = feed(monitor, [fail(), fail(), OK, fail(), fail()])

    assert [d.outcome for d in decisions] == [
        Outcome.STILL_BREACHING,
        Outcome.FAILURE_CONFIRMED,
        Outcome.RECOVERED,
        Outcome.STILL_BREACHING,   # counter started over
        Outcome.FAILURE_CONFIRMED,  # a brand-new incident
    ]
    assert monitor.incident_open is True


def test_recovery_threshold_requires_multiple_good_polls() -> None:
    monitor = BreachMonitor(breach_threshold=1, recovery_threshold=2)

    decisions = feed(monitor, [fail(), OK, OK])

    assert [d.outcome for d in decisions] == [
        Outcome.FAILURE_CONFIRMED,
        Outcome.NOTHING,     # 1 good poll — not enough to declare recovered yet
        Outcome.RECOVERED,   # 2nd good poll closes it
    ]


def test_a_failure_breaks_a_partial_recovery_streak() -> None:
    monitor = BreachMonitor(breach_threshold=1, recovery_threshold=2)

    decisions = feed(monitor, [fail(), OK, fail(), OK, OK])

    assert [d.outcome for d in decisions] == [
        Outcome.FAILURE_CONFIRMED,
        Outcome.NOTHING,   # 1/2 good
        Outcome.NOTHING,   # failure again — still the same open incident, streak reset
        Outcome.NOTHING,   # 1/2 good
        Outcome.RECOVERED,  # 2/2 good
    ]
    assert monitor.incident_open is False


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize("bad", [0, -1])
def test_breach_threshold_must_be_at_least_one(bad: int) -> None:
    with pytest.raises(ValueError):
        BreachMonitor(breach_threshold=bad)


@pytest.mark.parametrize("bad", [0, -5])
def test_recovery_threshold_must_be_at_least_one(bad: int) -> None:
    with pytest.raises(ValueError):
        BreachMonitor(breach_threshold=3, recovery_threshold=bad)
