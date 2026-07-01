# ANTON routines — dev task shortcuts.
#
# This repo has no build system that needs `make`; the canonical entry points
# are the [project.scripts] console commands (pyproject.toml) and `python -m`.
# This Makefile is a thin convenience wrapper so the documented
# `make lint-composites` works where `make` is installed. On Windows without
# `make`, run the underlying command directly in the routines venv:
#
#     python -m routines.composite.lint          # or: lint-composites
#
# Use the active venv's python; override with `make PY=... lint-composites`.
PY ?= python

.PHONY: lint-composites

# Composite-template linter (#steal-kocoro P2). Runs the same validator the
# bridge runs at boot over composites/*.orchestration.json; exits non-zero on
# any malformed composite (host-agent pinning, EXTRACT_JSON-after-TOOL, DAG
# acyclicity, per-step sensitivity <= composite max).
lint-composites:
	$(PY) -m routines.composite.lint
