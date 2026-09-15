"""FastAPI app: wires logging, starts the generator, exposes /health and /metrics.

Endpoints:
* ``GET /health``  — 200 only if BOTH Postgres and Redis round-trip; else 503 +
  a JSON body naming which dependency failed.
* ``GET /metrics`` — Prometheus exposition of the three custom metrics (plus the
  client library's default process metrics).
* ``GET /``        — tiny liveness ("the process answers HTTP"); intentionally
  weaker than /health.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app import cache, db, generator
from app.logging_setup import setup_logging
from app.metrics import ingestion_lag_seconds

logger = setup_logging()

# Module-level so the lifespan can start it and /health-adjacent code could reach
# it if ever needed.
_generator = generator.TickGenerator()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Wire the Gauge to a scrape-time callback: the lag is recomputed on every
    # Prometheus scrape from the last-write timestamp, so it keeps rising even if
    # the generator thread is wedged.
    ingestion_lag_seconds.set_function(generator.seconds_since_last_write)

    logger.info("starting feed-service", extra={"event": "startup"})
    _generator.start()  # blocks until Postgres + Redis are reachable (backoff)
    try:
        yield
    finally:
        logger.info("stopping feed-service", extra={"event": "shutdown"})
        _generator.stop()


app = FastAPI(title="pulsecheck feed-service", lifespan=lifespan)


@app.get("/")
def root() -> dict:
    # Weak on purpose: proves only that the web server is up. The real check is
    # /health. Keeping the distinction visible is itself an interview point.
    return {"service": "feed-service", "status": "alive"}


# NOTE: plain `def`, not `async def`. FastAPI runs sync routes in a threadpool,
# so the blocking psycopg2/redis calls here don't stall the event loop.
@app.get("/health")
def health() -> Response:
    """200 iff both dependencies round-trip; otherwise 503 with the reason."""
    result = {"status": "ok", "postgres": "ok", "redis": "ok"}
    status_code = 200

    try:
        db.ping()
    except Exception as exc:
        result["postgres"] = f"error: {exc}"
        result["status"] = "unhealthy"
        status_code = 503

    try:
        cache.ping()
    except Exception as exc:
        result["redis"] = f"error: {exc}"
        result["status"] = "unhealthy"
        status_code = 503

    if status_code != 200:
        logger.warning(
            "health check failed: %s", result,
            extra={"event": "health_unhealthy"},
        )
    return JSONResponse(result, status_code=status_code)


@app.get("/metrics")
def metrics() -> Response:
    """Prometheus scrape endpoint. Just renders current metric state — no locks,
    no dependency calls — so a scrape during a crash can't block or half-read."""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
