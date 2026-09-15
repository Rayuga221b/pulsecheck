"""pulsecheck watchdog — host process that watches the feed-service container.

Package layout (kept small and single-purpose per module so each piece is
independently explainable and independently testable):

* :mod:`watchdog.probes`  — the two health signals: an HTTP ``/health`` poll and
  the container's own state read over the Docker API. Each returns a plain
  verdict object; neither has any opinion about what to *do*.
* :mod:`watchdog.breach`  — the consecutive-breach state machine. Pure logic, no
  I/O: you feed it verdicts, it tells you when a failure is *confirmed* and when
  the target has *recovered*. This is the file to unit-test with fakes (M6).
* :mod:`watchdog.notify`  — formats and POSTs the Slack incoming-webhook message.
* :mod:`watchdog.__main__` — wires the three together in a poll loop and owns the
  side effects (restart the container, write the incident row, send Slack).
"""
