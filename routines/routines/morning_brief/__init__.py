"""Morning Brief — auto-generated overnight via local Ollama.

Replaces the hand-coded seed in the dashboard's MorningBriefPanel with a
real briefing pulled from vault state.

Pipeline:
    pull         → gather_needs_you  + gather_sector_this_week (from vault)
    synthesise   → qwen3:14b drafts an "Anton suggests" paragraph
    writer       → atomic write to Routines/morning-briefs/<date>.md
    reader       → load + parse for the bridge endpoint

Triggered by a scheduled task (or `morning-brief generate` CLI). Bridge
exposes `/api/morning-brief/today` which the dashboard reads on mount.

All steps are local: nothing leaves the operator's machine. Confidential
project context can land in the brief without sensitivity routing
concerns.
"""

from routines.morning_brief.schema import MorningBrief, BriefRow
from routines.morning_brief.pull import gather_context, ContextBundle
from routines.morning_brief.synthesise import anton_suggests, classify_actions
from routines.morning_brief.writer import write_brief
from routines.morning_brief.reader import load_today, load_for_date

__all__ = [
    "MorningBrief", "BriefRow", "ContextBundle",
    "gather_context", "anton_suggests", "classify_actions",
    "write_brief", "load_today", "load_for_date",
]
