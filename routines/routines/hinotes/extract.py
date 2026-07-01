"""LLM extraction prompts and parsing for HiNotes transcripts.

The single Ollama call returns a JSON object describing the meeting. Schema
mirrors the structured-note shape in `Templates/meeting-note.md`.

Why one big call rather than several small ones:
    - qwen3:14b is good at structured extraction in one shot
    - Multiple round-trips compound latency (each call is 10-30s on this
      hardware) without meaningfully improving quality
    - JSON-mode forces the model to produce valid output

Failure modes to handle:
    - JSON parse failure (model added prose preamble) — handled in
      ollama_client.parse_json_response
    - Missing fields — defaults applied here
    - Hallucinated dates / names — flagged as inferred via certainty markers
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from routines.shared.ollama_client import OllamaClient, OllamaError, parse_json_response

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """\
You are a structured-extraction assistant for an M&A / corporate-development
professional. You read meeting transcripts and produce a JSON object describing
the meeting.

Rules:
- Paraphrase, do not quote. Distil facts in your own words; do not echo
  verbatim sentences from the transcript.
- Mentions: include People (proper nouns referring to individuals) and
  Companies (proper nouns referring to organisations). Do not include
  generic words.
- Sensitivity classification: choose the most-restrictive accurate tier:
    * "MNPI"         = pre-announce results, embargoed news, inside info
    * "confidential" = deal codename, target/buyer name, signed NDA contents,
                       VDR docs, live pipeline
    * "internal"     = analysis on public material with no party named
    * "public"       = the call is about publicly-available information only
  Default to "confidential" for any deal-related conversation.
- Decisions and actions: include only those explicitly made in the call.
  Do not invent next steps the participants did not discuss.
- Issues: include only genuine risks, blockers, or items that must be tracked
  through the life of the deal (e.g. "FDD engagement letter still unsigned",
  "working-capital adjustment needs monitoring into the SPA"). Routine to-dos
  belong in actions, not issues. Empty list if none — most calls raise none.
- Dates: use YYYY-MM-DD format; if a date is not explicit, set to null.
- Currency / numbers: if cited, preserve denomination and period; do not
  perform arithmetic.

Reply with JSON only. No prose preamble. No markdown fences.
"""


SCHEMA_HINT = """\
Return a JSON object with this exact shape:
{
  "summary": "2-3 sentence high-level paraphrased summary of the meeting",
  "duration_minutes": <integer or null>,
  "meeting_date": "YYYY-MM-DD or null",
  "meeting_title": "concise title for the note (e.g. 'DemoCo management call')",
  "attendees": [
    {"name": "Full Name", "role": "role/firm if mentioned, else null"}
  ],
  "project_mentions": ["Project codename or company name", ...],
  "company_mentions": [
    {"name": "Company Name", "context": "1-line paraphrased context"}
  ],
  "people_mentions": [
    {"name": "Person Name", "context": "1-line paraphrased context"}
  ],
  "sector_mentions": ["Sector"],
  "key_facts": [
    {"fact": "paraphrased fact", "topic": "category if obvious, else null"}
  ],
  "decisions": [
    {"decision": "what was decided", "owner": "name or null", "date": "YYYY-MM-DD or null"}
  ],
  "actions": [
    {"action": "what to do", "owner": "name or null", "due": "YYYY-MM-DD or null"}
  ],
  "issues": [
    {"title": "short issue title", "why": "1 line on why it matters / what could bite",
     "affects": "downstream artefact(s) it must flow into (e.g. 'SPA - completion accounts') or null",
     "suggested_priority": "P1 | P2 | P3 or null",
     "gating_items": ["follow-up needed to resolve or verify", ...]}
  ],
  "open_questions": ["question 1", ...],
  "sensitivity_classification": "public | internal | confidential | MNPI",
  "sensitivity_rationale": "1 sentence explaining the choice"
}
"""


@dataclass
class Extraction:
    """Structured-extraction result. Maps 1:1 to meeting-note.md frontmatter + body."""

    summary: str = ""
    duration_minutes: int | None = None
    meeting_date: str | None = None
    meeting_title: str = "Untitled meeting"
    attendees: list[dict[str, Any]] = field(default_factory=list)
    project_mentions: list[str] = field(default_factory=list)
    company_mentions: list[dict[str, str]] = field(default_factory=list)
    people_mentions: list[dict[str, str]] = field(default_factory=list)
    sector_mentions: list[str] = field(default_factory=list)
    key_facts: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)  # #issues-register v1.5
    open_questions: list[str] = field(default_factory=list)
    sensitivity_classification: str = "confidential"
    sensitivity_rationale: str = ""


def extract_from_transcript(
    transcript: str,
    *,
    client: OllamaClient,
    model: str = "qwen3:14b",
) -> Extraction:
    """Run the structured extraction. Returns Extraction with sensible defaults
    for any field the model omits.

    Raises OllamaError on transport failure or unrecoverable JSON parse failure.
    """
    if not transcript.strip():
        raise ValueError("transcript is empty")

    # Truncate very long transcripts to keep within model context.
    # qwen3:14b context is generous (40k+ tokens) but extraction quality
    # drops on very long inputs. Cap at ~25k chars (~6k tokens) — covers
    # most 1-hour meeting transcripts.
    MAX_CHARS = 25_000
    if len(transcript) > MAX_CHARS:
        logger.warning(
            "transcript is %d chars; truncating to %d (last quarter dropped)",
            len(transcript), MAX_CHARS,
        )
        # Keep the start (intros, agenda) and the end (decisions, actions)
        head = transcript[: int(MAX_CHARS * 0.6)]
        tail = transcript[-int(MAX_CHARS * 0.4):]
        transcript = head + "\n\n[... middle truncated for length ...]\n\n" + tail

    prompt = f"{SCHEMA_HINT}\n\nTranscript:\n\n{transcript}"
    response = client.chat(
        model=model,
        prompt=prompt,
        system=SYSTEM_PROMPT,
        json_mode=True,
        temperature=0.2,
    )
    logger.info(
        "extraction model=%s tokens_eval=%s duration=%.1fs",
        response.model,
        response.eval_count,
        response.total_duration_seconds or 0,
    )

    try:
        data = parse_json_response(response.content)
    except OllamaError:
        logger.error("json parse failed; raw content first 500 chars: %s", response.content[:500])
        raise

    return _normalise(data)


def _normalise(data: dict[str, Any]) -> Extraction:
    """Apply defaults; coerce types; drop noise."""
    sensitivity = data.get("sensitivity_classification", "confidential")
    if sensitivity not in ("public", "internal", "confidential", "MNPI"):
        sensitivity = "confidential"

    return Extraction(
        summary=str(data.get("summary", "")).strip(),
        duration_minutes=_safe_int(data.get("duration_minutes")),
        meeting_date=_safe_date(data.get("meeting_date")),
        meeting_title=str(data.get("meeting_title", "Untitled meeting")).strip()[:120],
        attendees=_safe_list_of_dicts(data.get("attendees")),
        project_mentions=_safe_list_of_strs(data.get("project_mentions")),
        company_mentions=_safe_list_of_dicts(data.get("company_mentions")),
        people_mentions=_safe_list_of_dicts(data.get("people_mentions")),
        sector_mentions=_safe_list_of_strs(data.get("sector_mentions")),
        key_facts=_safe_list_of_dicts(data.get("key_facts")),
        decisions=_safe_list_of_dicts(data.get("decisions")),
        actions=_safe_list_of_dicts(data.get("actions")),
        issues=_safe_list_of_dicts(data.get("issues")),
        open_questions=_safe_list_of_strs(data.get("open_questions")),
        sensitivity_classification=sensitivity,
        sensitivity_rationale=str(data.get("sensitivity_rationale", "")).strip(),
    )


def _safe_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _safe_date(v: Any) -> str | None:
    if not v or not isinstance(v, str):
        return None
    v = v.strip()
    # Cheap validation — must look like YYYY-MM-DD
    if len(v) == 10 and v[4] == "-" and v[7] == "-":
        return v
    return None


def _safe_list_of_strs(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    return [str(x).strip() for x in v if x and isinstance(x, (str, int, float))]


def _safe_list_of_dicts(v: Any) -> list[dict[str, Any]]:
    if not isinstance(v, list):
        return []
    return [x for x in v if isinstance(x, dict)]
