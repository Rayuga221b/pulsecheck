"""Retry-with-exponential-backoff, used for both the Postgres and Redis connects.

Why this exists at all: docker-compose ``depends_on`` only controls the order
containers *start*, not whether the service inside is *ready*. Postgres needs a
second or two after its container is "up" before it accepts connections. Without
this, feed-service would lose the race on boot and exit. The same code path also
absorbs a dependency that dies and comes back mid-run.

Backoff schedule: 1s, 2s, 4s, 8s, ... each capped at ``cap`` seconds, for at most
``max_attempts`` tries. Capping matters — unbounded doubling would mean waiting
minutes between late attempts.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

T = TypeVar("T")


def connect_with_backoff(
    factory: Callable[[], T],
    *,
    what: str,
    max_attempts: int,
    cap_seconds: float,
    logger: logging.Logger,
) -> T:
    """Call ``factory`` until it succeeds or ``max_attempts`` is exhausted.

    ``factory`` is expected to raise on failure (that's how a driver signals it
    could not connect). On success its return value is passed straight back.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            result = factory()
            if attempt > 1:
                logger.info(
                    "connected to %s after %d attempts", what, attempt,
                    extra={"event": "dependency_connected"},
                )
            return result
        except Exception as exc:
            if attempt >= max_attempts:
                logger.error(
                    "giving up connecting to %s after %d attempts: %s", what, attempt, exc,
                    extra={"event": "dependency_unreachable"},
                )
                raise
            # 2 ** (attempt-1): 1, 2, 4, 8, ... then clamp to the cap.
            delay = min(2 ** (attempt - 1), cap_seconds)
            logger.warning(
                "%s not ready (attempt %d/%d): %s -- retrying in %.0fs",
                what, attempt, max_attempts, exc, delay,
                extra={"event": "dependency_retry"},
            )
            time.sleep(delay)
