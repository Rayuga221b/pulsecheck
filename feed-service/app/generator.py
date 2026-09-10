"""Synthetic tick generator — the ingestion write path.

Runs as a single daemon thread started on app startup. Loop:

    for each symbol:
        price <- random-walk step from its last price
        (timed) INSERT into Postgres  +  SET latest:<symbol> in Redis
        bump counter, refresh "last write" timestamp, log one JSON line
    sleep(tick_interval)

Design points to be able to defend:

* **Random walk**: ``price += price * gauss(0, STEP_VOLATILITY)``. It's a
  *simulated* feed; the goal is a believable wandering line, not a market model.
  ~0.1% per step, floored at a small positive number so price never hits <= 0.
  Uses ``random.gauss`` from the stdlib — no numpy.

* **One connection, owned by this thread** (psycopg2 connections aren't
  thread-safe). If a write raises, we log it, throw the connection away, and
  rebuild it with the same backoff helper used at startup — so a transient
  Postgres/Redis outage self-heals without a process restart.

* **Ingestion lag is computed at scrape time**, not pushed. We only record
  ``_last_write_ts`` here; ``seconds_since_last_write()`` is wired to the Gauge
  as a scrape callback in main.py. Result: even if this whole thread hangs, every
  Prometheus scrape still sees the lag climbing.
"""

from __future__ import annotations

import logging
import random
import threading
import time

from app import cache, db
from app.config import config
from app.metrics import request_latency_seconds, ticks_processed_total

log = logging.getLogger("feed-service.generator")

# Epoch seconds of the last successful write. Starts at "now" so lag reads ~0
# before the first tick rather than "seconds since 1970".
_last_write_ts: float = time.time()
_PRICE_FLOOR = 0.01


def seconds_since_last_write() -> float:
    """Scrape-time callback for the ingestion_lag_seconds Gauge."""
    return time.time() - _last_write_ts


class TickGenerator:
    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="tick-generator", daemon=True)
        # Each symbol walks independently from its own last price.
        self._prices: dict[str, float] = {s: config.start_price for s in config.symbols}
        self._pg = None  # set in _run once connected
        self._redis = None

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        self._pg = db.connect_with_retry()
        db.init_schema(self._pg)
        self._redis = cache.connect_with_retry()
        self._thread.start()
        log.info("tick generator started", extra={"event": "generator_start"})

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    # -- main loop ------------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            for symbol in config.symbols:
                price = self._next_price(symbol)
                try:
                    self._write(symbol, price)
                except Exception as exc:
                    # Log the failure as structured data and try to rebuild the
                    # broken connection(s). The loop keeps going.
                    log.error(
                        "tick write failed for %s: %s", symbol, exc,
                        extra={"event": "tick_write_error"}, exc_info=True,
                    )
                    self._reconnect()
            # Fixed cadence. Not drift-corrected on purpose — simple to explain,
            # and a few ms of skew per second does not matter for a demo feed.
            self._stop.wait(config.tick_interval_seconds)

    # -- helpers -----------------------------------------------------------
    def _next_price(self, symbol: str) -> float:
        last = self._prices[symbol]
        step = last * random.gauss(0.0, config.step_volatility)
        new_price = max(last + step, _PRICE_FLOOR)
        self._prices[symbol] = new_price
        return new_price

    def _write(self, symbol: str, price: float) -> None:
        """Timed write to both stores. The Histogram wraps the whole path."""
        global _last_write_ts
        with request_latency_seconds.time():
            db.write_tick(self._pg, symbol, price)
            cache.set_latest(self._redis, symbol, price)
        ticks_processed_total.inc()
        _last_write_ts = time.time()
        log.info(
            "wrote tick %s=%.4f", symbol, price,
            extra={"event": "tick_write"},
        )

    def _reconnect(self) -> None:
        """Rebuild both clients after a failure, reusing the startup backoff."""
        try:
            self._pg = db.connect_with_retry()
            self._redis = cache.connect_with_retry()
            log.info("reconnected to dependencies", extra={"event": "generator_reconnect"})
        except Exception as exc:
            log.error(
                "reconnect failed: %s", exc,
                extra={"event": "generator_reconnect_failed"}, exc_info=True,
            )
