"""Content-trust guards (#sec-injection-guard).

Inbound-text inspection at the ingestion boundary — the content-trust sibling of
the path-trust layer (``routines/shared/read_policy.py`` / ``write_policy.py``)
and distinct from the dispatch-time hook guards in
``routines/hooks/central_guards.py``. The first member is the prompt-injection
heuristic scanner (``routines.guards.injection``).
"""
