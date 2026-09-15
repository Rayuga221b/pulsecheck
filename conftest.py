"""Repo-root pytest configuration shared by every test package.

There is deliberately no monorepo tooling here (see CLAUDE.md): each component is
its own folder with its own ``requirements.txt``. The one thing the test run
needs that a plain ``pytest`` invocation does not give us is import paths:

* ``feed-service/`` is not a package and its code imports ``app.*`` — so
  ``feed-service/`` has to be on ``sys.path``.
* the ``watchdog`` and ``log_watcher`` packages both do ``import incidents``
  (the shared SQLite schema module at the repo root) — so the repo root has to
  be on ``sys.path`` too. Running pytest from the repo root already puts it
  there, but we add it explicitly so ``pytest tests/`` from elsewhere still works.

We also pin the environment variables the feed-service reads at *import time*
(``app.config`` builds a frozen singleton from ``os.environ`` the moment it is
imported, and ``app.logging_setup`` calls ``os.makedirs`` on ``LOG_FILE``'s
directory). Setting them here, before any test imports ``app.*``, keeps the
suite from touching a real Postgres/Redis or trying to create ``/app/logs``.
"""

from __future__ import annotations

import os
import sys
import tempfile

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
_FEED_SERVICE = os.path.join(_REPO_ROOT, "feed-service")

for path in (_REPO_ROOT, _FEED_SERVICE):
    if path not in sys.path:
        sys.path.insert(0, path)

# Point the feed-service log file at a throwaway location so importing the app
# under test never tries to create the container-only ``/app/logs`` directory.
_LOG_DIR = os.path.join(tempfile.gettempdir(), "pulsecheck-tests")
os.makedirs(_LOG_DIR, exist_ok=True)
os.environ.setdefault("LOG_FILE", os.path.join(_LOG_DIR, "feed-service.log"))

# Everything else the feed-service needs already has a safe default in
# ``app.config`` (localhost-ish hosts, test creds); the tests that exercise
# ``/health`` monkeypatch ``db.ping`` / ``cache.ping`` and never open a socket.
