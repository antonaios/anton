#!/usr/bin/env bash
# Convenience launcher for the HiNotes watcher.
# Run from anywhere; uses the ~/.venvs/agentic-routines venv.
#
# For always-on operation, prefer the systemd --user unit at
# scripts/hinotes-watcher.service (see scripts/README.md).
set -euo pipefail

VENV="${HOME}/.venvs/agentic-routines"
VAULT="${AGENTIC_VAULT:-/mnt/x/OS AI Vault}"

if [ ! -d "${VENV}" ]; then
    echo "venv not found at ${VENV}" >&2
    echo "create with: python3 -m venv ${VENV} && ${VENV}/bin/pip install -e \"/mnt/x/Agentic OS/routines[dev]\"" >&2
    exit 1
fi

# shellcheck disable=SC1091
source "${VENV}/bin/activate"

exec hinotes-watcher start --vault "${VAULT}"
