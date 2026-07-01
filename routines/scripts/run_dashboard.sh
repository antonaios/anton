#!/usr/bin/env bash
# Launch the Agentic OS dashboard MVP at http://127.0.0.1:8501.
# Localhost-bound only; no auth (loopback is the auth boundary).
set -euo pipefail

VENV="${HOME}/.venvs/agentic-routines"
REPO="/mnt/x/Agentic OS/routines"

if [ ! -d "${VENV}" ]; then
    echo "venv not found at ${VENV}" >&2
    exit 1
fi

# shellcheck disable=SC1091
source "${VENV}/bin/activate"

# Bind 0.0.0.0 so the WSL2 port forwarder reflects 8501 to Windows-localhost.
# This is still safe: WSL2's NAT firewall does not expose the port to the
# network — only Windows host processes can reach it, same security boundary
# as 127.0.0.1 on a non-WSL setup. Disable telemetry. Headless = no auto-browser.
exec streamlit run \
    "${REPO}/routines/dashboard/app.py" \
    --server.address=0.0.0.0 \
    --server.port=8501 \
    --server.headless=true \
    --browser.gatherUsageStats=false
