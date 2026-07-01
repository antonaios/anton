"""Digest crew bridge-side surface (#ingest-digest, slice 1: stages 1-2).

The crew itself (MetaGPT roles + the deterministic scanner / fail-closed
classifier / parallel analyzers) lives crew-side under
``crews_src/digest_crew.py`` + ``crews_src/_shared/digest/`` — it runs in the
isolated 3.11 crew venv and the bridge NEVER imports it. This package is only
the operator-attended CLI trigger (decision 3), which drives the crew through
the same gated path the HTTP route uses (``routines.crew``).
"""
