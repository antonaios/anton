"""FastAPI bridge — exposes the routines CLI surface over HTTP for the
React dashboard at <repo>/dashboard/.

Sensitivity-gating remains the responsibility of the underlying CLIs;
this package is a thin transport layer that binds to loopback only.
"""
