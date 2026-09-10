"""Structured (single-line JSON) logging.

The M3 log-watcher is a plain regex/field tailer running on the host. It reads a
*file*, one JSON object per line, and pulls fields out of it. So every log record
this service emits must be:

* exactly one line (no pretty-printing, no multi-line tracebacks by default),
* valid JSON,
* with a stable set of fields: ``ts``, ``level``, ``event``, ``detail``.

We attach two handlers:
* a FileHandler on the bind-mounted path (what the log-watcher tails),
* a StreamHandler to stdout (so ``docker logs`` still shows something useful).

Both use the same JSON formatter so the two streams never diverge.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

from app.config import config


class JsonLineFormatter(logging.Formatter):
    """Render each LogRecord as one compact JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            # `event` is a short machine-friendly tag passed via extra={"event": ...};
            # fall back to the logger name if a caller forgot to set one.
            "event": getattr(record, "event", record.name),
            # `detail` is the human-readable message.
            "detail": record.getMessage(),
        }
        # Only include a traceback when there actually is one, and flatten it to
        # a single string so the line stays one line.
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info).replace("\n", " | ")
        return json.dumps(payload, separators=(",", ":"))


def setup_logging() -> logging.Logger:
    """Configure the root logger once and return the app logger."""
    os.makedirs(os.path.dirname(config.log_file), exist_ok=True)

    formatter = JsonLineFormatter()

    file_handler = logging.FileHandler(config.log_file)
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(config.log_level.upper())
    # Clear any handlers uvicorn/others installed so we don't get duplicate or
    # non-JSON lines in our file.
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stream_handler)

    return logging.getLogger("feed-service")


def log_event(logger: logging.Logger, level: int, event: str, detail: str, **kw) -> None:
    """Helper so call sites read as: log_event(log, logging.INFO, "tick_write", "...")."""
    logger.log(level, detail, extra={"event": event, **kw})
