"""Crew lane — bridge-side scaffolding for MetaGPT autonomous crews (#31).

The crew lane mirrors the composite (Synapse) lane's process-boundary
discipline: MetaGPT lives in its OWN venv at ``<repo>\\crews\\.venv``
(Python 3.11 — MetaGPT supports 3.9-3.11, the bridge venv is 3.14) and the
bridge talks to it exclusively via ``subprocess`` + JSON-over-stdio.

**Hard rule: nothing under ``routines/`` imports metagpt.** The boundary is
``subprocess.Popen``; collapsing it collapses the swap-cost story (see
``proposed-2026-05-26-phase7/METAGPT-INTEGRATION-SPEC.md`` §0 and the
[[composite-skills]] §6 switch-readiness audit, which applies verbatim).
``tests/crew/test_crew_routes.py::test_bridge_does_not_import_metagpt`` locks
this invariant.

Modules:
  * ``types``        — bridge-side ``CrewInput`` / ``CrewOutput`` Pydantic
                       contract (manually kept in sync with
                       ``crews_src/_shared/boundary.py``; parity is tested).
  * ``proxy``        — subprocess launcher + stdout line-demuxer
                       (HumanProvider envelopes vs the final result line).
  * ``pid_store``    — run_id → PID map for ``/cancel`` (+ cancelled-flag).
  * ``registry``     — verb → manifest; sensitivity + lane resolution.
  * ``audit_mirror`` — parent + per-role audit rows via
                       ``audit.write_structured(routine=...)``.
  * ``install``      — ``install_metagpt.py``, the #26a-pattern installer
                       that creates the isolated crew venv and deploys the
                       crew-side sources from ``<repo>/crews_src/``.
"""
