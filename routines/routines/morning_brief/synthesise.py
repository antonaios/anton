"""Synthesise the 'Anton suggests' paragraph + categorise action items.

Two LLM calls (both local Ollama qwen3:14b):

1. **classify_actions** — takes the raw ActionItem list, returns each row
   tagged with marker (ovd / due / open) plus a clean one-line sub. The
   LLM is allowed to drop noisy items (false positives from the regex
   extractor).

2. **anton_suggests** — given the classified actions + sector news +
   profile context, draft a short reflective paragraph identifying the
   one or two things the operator should care about today and why.

Both calls are local. No confidential context leaves the machine.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import date

from routines.morning_brief.pull import ActionItem, ContextBundle
from routines.morning_brief.schema import BriefRow
from routines.shared.ollama_client import OllamaClient, OllamaError, parse_json_response

log = logging.getLogger(__name__)


DEFAULT_MODEL = "qwen3:14b"


# ── 1. Classify actions ───────────────────────────────────────────────────

_CLASSIFY_SYSTEM = """\
You are filtering raw action-item candidates extracted from M&A meeting
notes. Each input has: text, project, age_days, due_date (ISO or null),
source_path.

Return JSON only:
{
  "rows": [
    { "marker": "ovd" | "due" | "open", "text": "<short>", "sub": "<context>" },
    ...
  ]
}

Rules:
- "marker"=ovd if due_date < today, OR (no due_date AND age_days > 14).
- "marker"=due if due_date == today.
- "marker"=open otherwise.
- DROP items that are clearly meeting-prep boilerplate, agenda items,
  not real actions, or duplicates of higher-quality items in the list.
- "text" = the action stripped to a short imperative ("Send NDA to
  Heartwood Collection", not "Sending the NDA out to...").
- "sub" = one short context phrase: project name + age/due (e.g.
  "Falcon · due today · IC committee Tue", "Heartwood · overdue 27d",
  "Sage · +5d · Manual").
- Aim for 4-6 rows total. Cluster overdue items at the top.
- If no genuine action items, return {"rows": []}.

Today is {today}.
"""


def classify_actions(
    actions: list[ActionItem], *,
    today: date,
    client: OllamaClient,
    model: str = DEFAULT_MODEL,
) -> list[BriefRow]:
    if not actions:
        return []

    payload = json.dumps([_action_to_payload(a) for a in actions], indent=2)
    system = _CLASSIFY_SYSTEM.replace("{today}", today.isoformat())
    user = f"Action-item candidates:\n\n{payload}"

    try:
        resp = client.chat(
            model=model, prompt=user, system=system, json_mode=True,
        )
        data = parse_json_response(resp.content)
    except OllamaError as e:
        log.warning("morning-brief: classify_actions LLM call failed: %s", e)
        return _fallback_classify(actions, today=today)
    except Exception as e:  # noqa: BLE001
        log.warning("morning-brief: classify_actions parse failed: %s", e)
        return _fallback_classify(actions, today=today)

    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return _fallback_classify(actions, today=today)

    out: list[BriefRow] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        marker = str(r.get("marker", "open")).lower()
        if marker not in ("ovd", "due", "open"):
            marker = "open"
        text = str(r.get("text", "")).strip()
        sub = str(r.get("sub", "")).strip()
        if not text:
            continue
        out.append(BriefRow(marker=marker, text=text, sub=sub))   # type: ignore[arg-type]
    return out


def _action_to_payload(a: ActionItem) -> dict:
    return {
        "text": a.text,
        "project": a.project,
        "age_days": a.age_days,
        "due_date": a.due_date.isoformat() if a.due_date else None,
        "source_path": a.source_path,
    }


def _fallback_classify(actions: list[ActionItem], *, today: date) -> list[BriefRow]:
    """If the LLM call fails, deterministically format the top actions
    without trying to be clever — better than crashing."""
    rows: list[BriefRow] = []
    for a in actions[:6]:
        if a.due_date and a.due_date < today:
            marker = "ovd"
            sub_extra = f"overdue · {(today - a.due_date).days}d"
        elif a.due_date == today:
            marker = "due"
            sub_extra = "due today"
        else:
            marker = "open"
            sub_extra = f"+{a.age_days}d"
        sub = " · ".join(filter(None, [
            a.project, sub_extra, a.source_path.split("/", 1)[0]
        ]))
        rows.append(BriefRow(marker=marker, text=a.text, sub=sub))   # type: ignore[arg-type]
    return rows


# ── 2. Anton suggests ─────────────────────────────────────────────────────

_SUGGEST_SYSTEM = """\
You are Anton — the operator's M&A copilot. Given today's open actions
and a few upcoming sector events, write a short reflective paragraph
(2-4 sentences, 50-90 words) that identifies the ONE OR TWO things the
operator should think about today and why.

Voice rules:
- en-GB spelling.
- Hedge-light. Direct. Principal-side.
- No bullet lists. Plain prose only.
- Don't restate the data — synthesise. Connect dots between an open
  action and an upcoming event, or flag the most overdue item with
  consequence.
- If there's nothing material, say so plainly. Don't pad.

Return PLAIN TEXT only (no JSON, no markdown).
"""


def anton_suggests(
    actions: list[BriefRow],
    sector_news: list[BriefRow], *,
    profile_context: str = "",
    client: OllamaClient,
    model: str = DEFAULT_MODEL,
) -> str:
    """Draft the 'Anton suggests' paragraph."""
    actions_block = "\n".join(f"- [{r.marker}] {r.text} ({r.sub})" for r in actions) or "(none)"
    news_block = "\n".join(f"- {r.text} ({r.sub})" for r in sector_news) or "(none)"

    user = (
        f"Open actions for today:\n{actions_block}\n\n"
        f"Sector events this week:\n{news_block}\n\n"
        f"Operator profile context:\n{profile_context or '(none)'}\n\n"
        f"Write Anton's suggestion paragraph."
    )

    try:
        resp = client.chat(model=model, prompt=user, system=_SUGGEST_SYSTEM)
    except OllamaError as e:
        log.warning("morning-brief: anton_suggests LLM call failed: %s", e)
        return _fallback_suggest(actions, sector_news)
    return (resp.content or "").strip() or _fallback_suggest(actions, sector_news)


def _fallback_suggest(actions: list[BriefRow], sector_news: list[BriefRow]) -> str:
    """Deterministic fallback when the LLM is unreachable."""
    overdue = [r for r in actions if r.marker == "ovd"]
    due = [r for r in actions if r.marker == "due"]
    if overdue:
        return f"{len(overdue)} overdue item{'s' if len(overdue) != 1 else ''} — clear the oldest before today's work. Highest: {overdue[0].text}."
    if due:
        return f"{len(due)} item{'s' if len(due) != 1 else ''} due today. Start with {due[0].text}."
    if sector_news:
        return f"No urgent actions. Worth scanning {sector_news[0].text} from this week's sector feed."
    return "Nothing material on the desk. Use the slack to chase a stale thread or do reading."


# Suppress unused-import warning
_ = asdict
