"""Endpoint tests for the feed-service HTTP surface.

Scope (from CLAUDE.md M6): ``/health`` returns 200 only when *both* dependencies
round-trip and 503 + a JSON reason when either is down, and ``/metrics`` exposes
exactly the three custom series with the right Prometheus types.

How the isolation works:

* ``TestClient(app)`` is used **without** the ``with`` block, so Starlette does
  not run the app's ``lifespan``. That means the background tick generator is
  never started and no real Postgres/Redis connection is ever attempted.
* ``/health`` calls ``db.ping()`` / ``cache.ping()`` by attribute lookup on the
  module at request time, so each test swaps those for a fake that either
  returns (dependency "up") or raises (dependency "down").
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import cache, db
from app.main import app

client = TestClient(app)


def _ok() -> None:
    """Stand-in for a dependency ping that succeeds (returns None, like the real one)."""
    return None


def _down(message: str):
    """Build a ping stand-in that fails the way the real driver would — by raising."""

    def _raise() -> None:
        raise RuntimeError(message)

    return _raise


@pytest.fixture(autouse=True)
def _both_dependencies_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default every test to 'Postgres and Redis are reachable'.

    Individual tests override one side with ``monkeypatch`` to simulate an outage.
    autouse so no test accidentally hits a real socket.
    """
    monkeypatch.setattr(db, "ping", _ok)
    monkeypatch.setattr(cache, "ping", _ok)


# --------------------------------------------------------------------------- /health


def test_health_ok_when_both_dependencies_round_trip() -> None:
    resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body == {"status": "ok", "postgres": "ok", "redis": "ok"}


def test_health_503_with_reason_when_postgres_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "ping", _down("could not connect to server"))

    resp = client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    # The failing dependency names itself and carries the driver error text...
    assert body["postgres"].startswith("error: ")
    assert "could not connect to server" in body["postgres"]
    # ...while the healthy one is still reported as ok.
    assert body["redis"] == "ok"


def test_health_503_with_reason_when_redis_down(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cache, "ping", _down("Error 111 connecting to redis:6379"))

    resp = client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["redis"].startswith("error: ")
    assert "redis:6379" in body["redis"]
    assert body["postgres"] == "ok"


def test_health_503_when_both_down_reports_both(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db, "ping", _down("pg gone"))
    monkeypatch.setattr(cache, "ping", _down("redis gone"))

    resp = client.get("/health")

    assert resp.status_code == 503
    body = resp.json()
    assert "pg gone" in body["postgres"]
    assert "redis gone" in body["redis"]


# --------------------------------------------------------------------------- /metrics


def test_metrics_exposes_the_three_custom_series_with_correct_types() -> None:
    body = client.get("/metrics").text

    # Gauge — a value that moves both directions (seconds since last write).
    assert "# TYPE ingestion_lag_seconds gauge" in body
    # Counter — monotonic total; prometheus_client renders it as <name>_total.
    assert "# TYPE ticks_processed_total counter" in body
    assert "ticks_processed_total " in body
    # Histogram — latency distribution; brings _bucket / _count / _sum with it.
    assert "# TYPE request_latency_seconds histogram" in body
    assert "request_latency_seconds_bucket{" in body
    assert "request_latency_seconds_count " in body


def test_metrics_endpoint_is_prometheus_content_type() -> None:
    resp = client.get("/metrics")

    assert resp.status_code == 200
    # text/plain; version=0.0.4 — the Prometheus exposition format.
    assert resp.headers["content-type"].startswith("text/plain")


# --------------------------------------------------------------------------- /


def test_root_is_a_weak_liveness_probe_only() -> None:
    """`/` proves only that the web server answers — deliberately weaker than
    `/health`, which is the readiness check. Keeping the two distinct is itself a
    talking point, so it gets a test."""
    resp = client.get("/")

    assert resp.status_code == 200
    assert resp.json() == {"service": "feed-service", "status": "alive"}
