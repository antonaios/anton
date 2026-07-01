"""Localhost dashboard MVP for the Agentic OS.

Streamlit-based, single-file at app.py. Six panels per plan §8:
  - Active project + sensitivity selector
  - Skill launcher (fires Claude Code slash commands)
  - Recall query box (with --synthesise option)
  - Recent HiNotes runs (audit log reader)
  - Vault pulse (recently-modified notes)
  - Routine status (Ollama health, watcher running, index freshness)

Run: `bash scripts/run_dashboard.sh` or `streamlit run routines/dashboard/app.py`.
Localhost only — bind to 127.0.0.1, no auth (loopback is the auth boundary).

This is the MVP. Polished React replacement comes in W7-8.
"""
