"""Deterministic digest-crew helpers (#ingest-digest, slice 1: stages 1-2).

Runs in the ISOLATED crew venv (``<repo>\\crews\\.venv``, Python 3.11)
alongside ``digest_crew.py``. Every module here imports ONLY stdlib + pydantic
at module top (heavy deps — pypdfium2 / python-docx — are deferred INSIDE the
functions that need them, mirroring ``routines/intake/pdf_render.py``), so the
bridge-side test suite can load them by file path and exercise the
safety-critical classifier in the Python-3.14 bridge venv WITHOUT a crew venv
or a live Ollama — exactly the pattern ``tests/crew/test_crew_routes.py``
already uses for ``_shared/boundary.py``.

Layer split carried over from the Understand-Anything pipeline shape this crew
steals (INGEST-DIGEST-CREW-DECISION-2026-06-10.md §2): ``scanner`` + the parse
half of ``analyzer`` + the whole ``classifier`` are DETERMINISTIC and
reproducible; LLM judgment is confined to the enrichment half of ``analyzer``.
"""
