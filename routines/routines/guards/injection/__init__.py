"""Prompt-injection / content-trust guard (#sec-injection-guard 3a).

Detect-and-audit at the ingestion boundary; **NEVER blocks** in 3a (graduating
high-confidence detections to a fail-closed block is 3b, gated on real-traffic
false-positive tuning + a Shannon re-run). The heuristic detector
(``heuristics``) is a faithful, dependency-free port of protectai/rebuff's
Apache-2.0 heuristics; the orchestration + audit (``scan``) wraps it so a
scanner failure can never break ingestion.
"""

from routines.guards.injection.scan import InjectionVerdict, scan_ingested_text

__all__ = ["InjectionVerdict", "scan_ingested_text"]
