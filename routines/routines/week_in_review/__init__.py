"""Weekly week-in-review routine (#38).

Cron-driven (Mon 07:30 Europe/London), write-only draft generator. Each
run scans the past week across every repo (git commits), the audit JSONL
window, the OUTSTANDING.md ARCHIVE diff, and the LLM telemetry roll-up,
then renders a DRAFT markdown to ``Resources/Week-in-Review/<YYYY-Www>.md``
in the vault for the operator to review + edit + commit.

Most sections are mechanical aggregation (``collect.py``); the
"Decisions locked" + "Honest reflection" sections are synthesised by a
LOCAL Ollama model (``render.py``) with a deterministic fallback so a
model outage never loses the week's mechanical data. Same shape as
``routines.morning_brief`` / ``routines.daily_digest``.

Output always carries a ``status: DRAFT`` banner — the routine never
auto-publishes. See OUTSTANDING.md #38.
"""

from __future__ import annotations
