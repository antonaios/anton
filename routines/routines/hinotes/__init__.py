"""HiNotes transcript ingestion routine.

Real-time watchdog daemon that watches `Inbox/HiNotes/incoming/`. When a
new transcript appears: hashes for idempotency, classifies sensitivity,
extracts structured note via local Ollama (qwen3:14b), routes to the right
project / Inbox/Captures, auto-stubs People/Companies cross-references,
moves verbatim transcript (converted to .md) to processed/.

Entry point: `routines.hinotes.watcher.main`. Installable as the
`hinotes-watcher` CLI script via pyproject.toml.
"""
