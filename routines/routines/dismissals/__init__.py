"""Inbox dismissals — separate SQLite-backed entity (#62).

Today's #8 proposal-rejection / skip flow writes sidecar files
(``<file>.skip.json`` and ``<file>.rejected.json``). That works for the
file-system-of-record model, but isn't queryable as a single "dismissed"
surface. Single-operator ANTON benefits especially — the operator IS the
audit, and "did I bury this last week?" has no first-class answer
without a dismissals log.

This package adds a queryable index alongside the existing sidecars:
sidecars stay source-of-truth for skip-expiry semantics + reject audit
trail; the SQLite table at ``routines/state/dismissals.db`` is the
queryable index that powers ``GET /api/dismissals`` + ``POST
/api/dismissals/{id}/undo``.

Two-write pattern: every reject + skip writes BOTH the sidecar AND a
dismissals row. Sidecar is the authoritative artefact (the proposal
scanner only consults sidecars); the row is the operator-facing audit
surface.
"""

from routines.dismissals.storage import (
    Dismissal,
    DismissalNotFound,
    DismissalAlreadyUndone,
    record_dismissal,
    get_dismissal,
    query_dismissals,
    mark_undone,
)

__all__ = [
    "Dismissal",
    "DismissalNotFound",
    "DismissalAlreadyUndone",
    "record_dismissal",
    "get_dismissal",
    "query_dismissals",
    "mark_undone",
]
