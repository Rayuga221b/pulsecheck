"""The three Prometheus metrics for the ingestion path — and nothing else.

Project constraint: exactly three custom metrics. Each type is chosen
deliberately; be able to justify it:

* ``ingestion_lag_seconds`` — **Gauge**.
  A value that moves both directions: "seconds since the last successful tick
  write". After every write we set it to ~0; if writes stop it climbs on every
  scrape. A Counter (monotonic) literally cannot represent a value that falls
  back down, so Gauge is the only correct choice. This is the headline
  "is ingestion alive?" signal.

* ``ticks_processed_total`` — **Counter**.
  Monotonically increasing total number of ticks written. You never graph the
  raw number; you graph ``rate(ticks_processed_total[1m])`` for "ticks/sec".
  Counter + rate() is the canonical "how often is this happening" pattern, and
  Prometheus knows how to handle the reset back to 0 on a process restart.

* ``request_latency_seconds`` — **Histogram**.
  Latency is a distribution. A Histogram buckets observations in-process so
  Prometheus can compute ``histogram_quantile(0.95, ...)`` server-side AND
  aggregate across instances. A Summary computes quantiles client-side and those
  quantiles cannot be aggregated later — wrong tool for a fleet.
  Buckets below are tuned for a fast local DB write (sub-millisecond to ~1s);
  the top bucket catches "something is very wrong".
"""

from prometheus_client import Counter, Gauge, Histogram

ingestion_lag_seconds = Gauge(
    "ingestion_lag_seconds",
    "Seconds since the last successful tick write (0 right after a write).",
)

ticks_processed_total = Counter(
    "ticks_processed_total",
    "Total number of ticks successfully written to Postgres + Redis.",
)

request_latency_seconds = Histogram(
    "request_latency_seconds",
    "Latency of the ingestion write path (Postgres INSERT + Redis SET).",
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)
