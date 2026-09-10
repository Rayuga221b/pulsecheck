"""Redis access: latest price per symbol only.

Access pattern: "what is the current price of AAPL?" — one key, read constantly,
and it is fine to lose this on a restart because the generator immediately writes
a fresh value. That is an in-memory key/value store, not a relational one.

Keys look like ``latest:AAPL`` -> stringified price. A single flat namespace,
one key per symbol, so a consumer can ``GET latest:AAPL`` or ``MGET`` the lot.

Unlike psycopg2, ``redis.Redis`` is thread-safe: it manages an internal
connection pool, so one shared client instance is fine for both the generator
thread and /health.
"""

from __future__ import annotations

import logging

import redis

from app.config import config
from app.retry import connect_with_backoff

log = logging.getLogger("feed-service.cache")

_KEY_PREFIX = "latest:"


def _new_client() -> "redis.Redis":
    client = redis.Redis(
        host=config.redis_host,
        port=config.redis_port,
        socket_connect_timeout=config.connect_timeout_seconds,
        socket_timeout=config.connect_timeout_seconds,
        decode_responses=True,  # get str back, not bytes
    )
    # redis.Redis is lazy — force an actual round-trip now so connect_with_retry
    # can see whether Redis is really up.
    client.ping()
    return client


def connect_with_retry() -> "redis.Redis":
    return connect_with_backoff(
        _new_client,
        what="redis",
        max_attempts=config.connect_max_attempts,
        cap_seconds=config.connect_backoff_cap_seconds,
        logger=log,
    )


def set_latest(client: "redis.Redis", symbol: str, price: float) -> None:
    """Overwrite the latest price for one symbol. Raises on failure."""
    client.set(f"{_KEY_PREFIX}{symbol}", repr(price))


def ping() -> None:
    """Round-trip check for /health. Opens its own short-lived client, PINGs, done.

    Mirrors db.ping(): /health exercises a real connect + round-trip to each
    dependency rather than trusting the generator's cached client.
    """
    client = _new_client()
    try:
        client.ping()
    finally:
        client.close()
