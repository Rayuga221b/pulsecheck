"""All runtime configuration, read from environment variables exactly once.

Why a single frozen dataclass instead of scattered ``os.getenv`` calls:
* One place to see every knob the service has.
* Parsing/validation happens at import time, so a bad value fails fast on
  startup instead of halfway through the first tick.
* ``frozen=True`` means nothing mutates config after boot — easier to reason about.

There is no default for secrets/hostnames that must be correct per environment;
there ARE defaults for tuning knobs so local runs work with an empty .env.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _get(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value


@dataclass(frozen=True)
class Config:
    # --- tick generation ---
    tick_interval_seconds: float
    symbols: tuple[str, ...]
    start_price: float
    step_volatility: float

    # --- Postgres ---
    pg_host: str
    pg_port: int
    pg_db: str
    pg_user: str
    pg_password: str

    # --- Redis ---
    redis_host: str
    redis_port: int

    # --- connection retry (shared by Postgres + Redis) ---
    connect_max_attempts: int
    connect_backoff_cap_seconds: float
    connect_timeout_seconds: float

    # --- logging ---
    log_file: str
    log_level: str

    @staticmethod
    def from_env() -> "Config":
        return Config(
            tick_interval_seconds=float(_get("TICK_INTERVAL_SECONDS", "0.25")),
            # split on comma, trim whitespace, drop empties
            symbols=tuple(
                s.strip() for s in _get("SYMBOLS", "AAPL,GOOG,MSFT,AMZN").split(",") if s.strip()
            ),
            start_price=float(_get("START_PRICE", "100.0")),
            step_volatility=float(_get("STEP_VOLATILITY", "0.001")),
            pg_host=_get("POSTGRES_HOST", "postgres"),
            pg_port=int(_get("POSTGRES_PORT", "5432")),
            pg_db=_get("POSTGRES_DB", "pulsecheck"),
            pg_user=_get("POSTGRES_USER", "pulsecheck"),
            pg_password=_get("POSTGRES_PASSWORD", "pulsecheck"),
            redis_host=_get("REDIS_HOST", "redis"),
            redis_port=int(_get("REDIS_PORT", "6379")),
            connect_max_attempts=int(_get("CONNECT_MAX_ATTEMPTS", "10")),
            connect_backoff_cap_seconds=float(_get("CONNECT_BACKOFF_CAP_SECONDS", "8")),
            connect_timeout_seconds=float(_get("CONNECT_TIMEOUT_SECONDS", "3")),
            log_file=_get("LOG_FILE", "/app/logs/feed-service.log"),
            log_level=_get("LOG_LEVEL", "INFO"),
        )


# Import-time singleton. Everything else does `from app.config import config`.
config = Config.from_env()
