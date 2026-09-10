"""Shared incident-log schema.

Both the log-watcher (M3) and the watchdog (M4) record incidents, and both must
agree on the table layout. Defining the schema *once* here — imported by both —
means there is exactly one ``CREATE TABLE`` in the codebase and no chance of the
two writers drifting apart.

Design choices worth explaining:

* **SQLite, not Postgres.** The incident log is written by *host* processes that
  observe the containers from the outside. Making them depend on the very
  Postgres container they might be reporting as down would be circular. SQLite is
  a single file on the host with no server to babysit.
* **Timestamps are ISO-8601 UTC strings.** SQLite has no native datetime type;
  storing ``2026-09-10T14:03:11Z`` keeps rows human-readable, sorts correctly as
  text, and is unambiguous across timezones.
* **``resolved_at`` NULL means the incident is still open.** Recovery is a second
  UPDATE that stamps ``resolved_at``; incident duration = ``resolved_at`` minus
  ``detected_at``, which is what the Discord message reports.
"""

import os
import sqlite3
from datetime import datetime, timezone

# Path is configurable so tests can point at a throwaway file and the EC2 box can
# put it somewhere predictable. Default: a file in the current working directory.
INCIDENTS_DB_PATH = os.getenv("INCIDENTS_DB_PATH", "incidents.db")

# One canonical schema. Columns mirror the architecture note in CLAUDE.md
# (timestamp, component, detected_via, action_taken, resolved_at) plus a
# free-text ``symptom`` so the Discord alert can say *what* was observed.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at  TEXT    NOT NULL,          -- ISO-8601 UTC, first confirmed failure
    component    TEXT    NOT NULL,          -- e.g. 'feed-service'
    detected_via TEXT    NOT NULL,          -- 'health' | 'container' | 'log-watcher'
    symptom      TEXT    NOT NULL,          -- human-readable description of the fault
    action_taken TEXT,                      -- e.g. 'restarted container'; NULL if none yet
    resolved_at  TEXT                       -- ISO-8601 UTC; NULL while the incident is open
);
"""


def connect(db_path: str = INCIDENTS_DB_PATH) -> sqlite3.Connection:
    """Open a connection with sensible defaults for multi-writer host scripts.

    ``check_same_thread=False`` because the watchdog may touch the DB from a
    worker thread; callers are still responsible for not sharing a single
    connection object across threads without their own locking.
    """
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # WAL lets a reader (e.g. a quick `sqlite3 incidents.db` during the demo)
    # run while a writer holds the DB, instead of getting "database is locked".
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init(db_path: str = INCIDENTS_DB_PATH) -> None:
    """Create the table if it does not exist. Safe to call on every startup."""
    conn = connect(db_path)
    try:
        conn.execute(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


def utcnow_iso() -> str:
    """ISO-8601 UTC with a trailing ``Z`` — the timestamp format every row uses."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def open_incident(
    component: str,
    detected_via: str,
    symptom: str,
    *,
    detected_at: str | None = None,
    action_taken: str | None = None,
    db_path: str = INCIDENTS_DB_PATH,
) -> int:
    """Insert one open incident row (``resolved_at`` left NULL) and return its id.

    Kept here rather than in the log-watcher / watchdog so the INSERT column list
    lives next to the ``CREATE TABLE`` and the two writers can't drift. The
    watchdog will add the matching ``resolve_incident`` (the UPDATE side) in M4.
    """
    detected_at = detected_at or utcnow_iso()
    conn = connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO incidents "
            "(detected_at, component, detected_via, symptom, action_taken) "
            "VALUES (?, ?, ?, ?, ?)",
            (detected_at, component, detected_via, symptom, action_taken),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


if __name__ == "__main__":
    # `python incidents.py` bootstraps the DB file for manual inspection.
    init()
    print(f"initialised incident schema in {INCIDENTS_DB_PATH}")
