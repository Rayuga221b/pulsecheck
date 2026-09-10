"""Postgres access: the durable tick history.

Access pattern this store serves: append a row per tick, later query ranges of
history. That's relational, disk-backed, must-survive-restart data -> Postgres on
a named volume. (The "what is AAPL's price right now" lookup is Redis's job, see
cache.py.)

Threading note: a psycopg2 connection object is NOT safe to share across threads.
So there are two independent connections in this service:
* the generator thread owns one long-lived connection (created via
  ``connect_with_retry``) and is the only writer;
* ``ping()`` opens its own short-lived connection per call for /health, so the
  health check exercises a real connect + query and never touches the writer's
  connection.
"""

from __future__ import annotations

import logging

import psycopg2

from app.config import config
from app.retry import connect_with_backoff

log = logging.getLogger("feed-service.db")

# Table is created on startup. TIMESTAMPTZ so timezone is explicit; an index on
# (symbol, ts) because every history query is "symbol X over a time range".
_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    id     BIGSERIAL PRIMARY KEY,
    symbol TEXT        NOT NULL,
    price  DOUBLE PRECISION NOT NULL,
    ts     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_ticks_symbol_ts ON ticks (symbol, ts DESC);
"""


def _new_connection(connect_timeout: float | None = None) -> "psycopg2.extensions.connection":
    """Open one Postgres connection.

    ``connect_timeout`` bounds how long a connect attempt hangs when Postgres is
    unreachable — this is what makes /health fail *fast* with a reason instead of
    blocking forever. autocommit=True: each tick INSERT is its own transaction,
    which is what we want for an append-only log.
    """
    conn = psycopg2.connect(
        host=config.pg_host,
        port=config.pg_port,
        dbname=config.pg_db,
        user=config.pg_user,
        password=config.pg_password,
        connect_timeout=int(connect_timeout or config.connect_timeout_seconds),
    )
    conn.autocommit = True
    return conn


def connect_with_retry() -> "psycopg2.extensions.connection":
    """Long-lived connection for the generator thread, waiting out a slow Postgres."""
    return connect_with_backoff(
        _new_connection,
        what="postgres",
        max_attempts=config.connect_max_attempts,
        cap_seconds=config.connect_backoff_cap_seconds,
        logger=log,
    )


def init_schema(conn: "psycopg2.extensions.connection") -> None:
    with conn.cursor() as cur:
        cur.execute(_SCHEMA)


def write_tick(conn: "psycopg2.extensions.connection", symbol: str, price: float) -> None:
    """Insert one tick. Raises on failure so the caller can log + reconnect."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ticks (symbol, price) VALUES (%s, %s)",
            (symbol, price),
        )


def ping() -> None:
    """Round-trip check for /health. Opens its own connection, SELECT 1, closes.

    Raises if Postgres is unreachable or the query fails; /health turns that into
    a 503 with a reason.
    """
    conn = _new_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    finally:
        conn.close()
