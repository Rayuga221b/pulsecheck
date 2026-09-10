"""Polling tail of a growing log file.

Why polling and not ``inotify`` / the ``watchdog`` library:

* **Portability & zero deps.** ``select``-based inotify is Linux-only; the
  ``watchdog`` package is a dependency and a moving part. A 1-second poll of a
  local file is completely adequate for an incident watcher — we are not chasing
  microseconds, we are noticing an outage that has already lasted seconds.
* **Explainable line by line.** "Open the file, remember the byte offset, every
  second read from the offset to EOF, split on newlines" is something I can
  whiteboard. inotify event coalescing is not.

Things it has to get right:

* **The file may not exist yet** when the watcher starts (compose still coming
  up). Wait for it instead of crashing.
* **Partial last line.** A read can land mid-line if the writer hasn't flushed
  the newline yet. Buffer anything after the last ``\\n`` and prepend it to the
  next read.
* **Truncation / rotation.** If the file's size drops below our offset, the file
  was rotated or truncated — seek back to 0 so we don't skip the new content or
  read garbage.
"""

from __future__ import annotations

import os
import time
from typing import Iterator


class FileTailer:
    def __init__(self, path: str, poll_seconds: float = 1.0, from_start: bool = False) -> None:
        self.path = path
        self.poll_seconds = poll_seconds
        self.from_start = from_start
        self._offset = 0
        self._carry = ""  # bytes read after the last newline, not yet a full line

    def follow(self) -> Iterator[str]:
        """Yield complete log lines (without the trailing newline) forever.

        Blocks between polls. Intended to be the outer loop of the watcher.
        """
        self._wait_for_file()
        # Start at EOF by default: on a fresh start we care about what happens
        # *from now on*, not the backlog. `from_start=True` (tests, replay of a
        # captured file) reads the whole thing.
        self._offset = 0 if self.from_start else os.path.getsize(self.path)

        while True:
            yield from self._read_new_lines()
            time.sleep(self.poll_seconds)

    def read_once(self) -> Iterator[str]:
        """Yield every complete line from the start of the file, then stop.

        Used by ``--replay`` to run the rules over a captured log and exit —
        this is what the M3 checkpoint drives.
        """
        self.from_start = True
        self._wait_for_file()
        self._offset = 0
        yield from self._read_new_lines()
        if self._carry.strip():
            # A final line with no trailing newline still counts in replay mode.
            yield self._carry
            self._carry = ""

    # -- internals --------------------------------------------------------

    def _wait_for_file(self) -> None:
        while not os.path.exists(self.path):
            time.sleep(self.poll_seconds)

    def _read_new_lines(self) -> Iterator[str]:
        try:
            size = os.path.getsize(self.path)
        except FileNotFoundError:
            # Rotated out from under us; wait for the replacement.
            self._wait_for_file()
            self._offset = 0
            self._carry = ""
            return

        if size < self._offset:
            # File shrank -> truncated or rotated. Restart from the top.
            self._offset = 0
            self._carry = ""

        if size == self._offset:
            return  # nothing new

        with open(self.path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(self._offset)
            chunk = fh.read()
            self._offset = fh.tell()

        data = self._carry + chunk
        parts = data.split("\n")
        # Everything except the last element is a complete line; the last element
        # is whatever came after the final newline (often "").
        self._carry = parts.pop()
        for line in parts:
            if line.strip():
                yield line
