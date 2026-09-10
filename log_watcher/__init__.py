"""pulsecheck log-watcher — "grep with memory".

A host-side process (NOT containerised — it observes the feed-service from the
outside, the same way an L1 on-call would) that polls the feed-service JSON log
file, applies a few field/regex rules with a little rolling-window state, and
writes an ``incidents`` row + a stdout line when a rule trips.

It is deliberately not a log platform: no Loki, no Promtail, no parser DSL. Three
small pieces:

* ``tailer.FileTailer``  — polling tail of a growing file, rotation-aware.
* ``rules``              — the detection rules and their windowed state.
* ``__main__``           — glue: tail -> parse JSON -> feed rules -> record hits.
"""
