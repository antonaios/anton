"""Detect follow-up feedback events from Claude Code session logs.

A "follow-up event" is a user message in a session that:
  - Comes shortly after an assistant message (within an hour, same session),
  - AND either: matches a "follow-up signal" regex (`what about`, `can you
    also`, `I (also)? need`, `missing`, `you didn't include`, etc.), OR is
    a short, declarative question (≤ 25 words) that doesn't already echo
    the previous topic.

This is a heuristic — it'll have false positives. The clustering step
filters them out: only patterns that recur across multiple sessions and
multiple artifacts survive.

We also try to identify the prior_artifact (a path mentioned in the
last assistant message) so we know which template/deliverable shape
the feedback applies to.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from routines.learning.schema import FeedbackEvent

log = logging.getLogger(__name__)


# ── Heuristic signals ─────────────────────────────────────────────────────

_FOLLOWUP_PATTERNS = [
    r"\bwhat\s+about\b",
    r"\bcan\s+you\s+(also\s+)?(?:add|include|tell|show|find|pull)\b",
    r"\bcould\s+you\s+also\b",
    r"\bplease\s+(also\s+)?(?:add|include)\b",
    r"\bi\s+(also\s+)?(?:need|want|expect)\b",
    r"\bmissing\b",
    r"\byou\s+didn'?t\s+(?:include|mention|cover|show)\b",
    r"\bshould(?:n'?t)?\s+(?:also\s+)?(?:include|have|show)\b",
    r"\bwhere(?:'?s|\s+is)\b.*\?",          # "where's the X?"
    r"\bwhy\s+(?:isn'?t|is\s+there\s+no)\b", # "why isn't there X?"
    r"\b(this|the\s+deck|the\s+profile|the\s+memo)\s+(should|needs?)\s+(?:to\s+)?(?:include|have|show)\b",
    r"\balways\s+include\b",
    r"\bnext\s+time\b.*\b(?:include|add|show)\b",
]

_FOLLOWUP_RE = re.compile("|".join(_FOLLOWUP_PATTERNS), re.IGNORECASE)


# Path-mention pattern (rough — captures "Wrote: Foo/bar.md", "wrote to
# Companies/X.md", "at `Templates/Y.md`", etc.).
_PATH_RE = re.compile(
    r"(?:Wrote[^\n]*?|wrote to|at|→|->|in|file:)\s*`?"
    r"((?:[A-Za-z0-9_]+/){0,4}[A-Za-z0-9_ .,&'()\-]+\.(?:md|xlsx|docx|pdf))",
    re.IGNORECASE,
)


# Map a vault path to an artifact kind based on directory.
_KIND_MAP = [
    ("Templates/company-profile.md", "company-profile-template"),
    ("Templates/ic-memo.md", "ic-memo-template"),
    ("Templates/one-pager.md", "one-pager-template"),
    ("Templates/investment-proposal.md", "investment-proposal-template"),
    ("Companies/", "company-profile"),
    ("Projects/", "project-deliverable"),
    ("Resources/Newsletters/", "newsletter"),
    ("Routines/morning-briefs/", "morning-brief"),
    ("Daily/", "daily-note"),
]


def classify_event(text: str, prior_assistant: str | None) -> tuple[str | None, str | None]:
    """Return (artifact_path, artifact_kind) inferred from the most recent
    assistant message that preceded the user's follow-up. Either may be None.
    """
    if not prior_assistant:
        return None, None
    m = _PATH_RE.search(prior_assistant)
    if not m:
        return None, None
    path = m.group(1).strip()
    kind = None
    for prefix, k in _KIND_MAP:
        if path.startswith(prefix) or prefix in path:
            kind = k
            break
    return path, kind


def looks_like_followup(text: str) -> bool:
    """Heuristic: does this user message look like a follow-up complaint /
    addition request rather than a fresh task?"""
    text = (text or "").strip()
    if not text or len(text) < 5:
        return False
    # Long instructions tend not to be follow-up complaints.
    word_count = len(text.split())
    if word_count > 35:
        return False
    if _FOLLOWUP_RE.search(text):
        return True
    return False


# ── Walk the session logs ────────────────────────────────────────────────


def _claude_projects_dir() -> Path:
    env = os.environ.get("CLAUDE_PROJECTS_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude" / "projects"


def _iter_jsonl_records(path: Path) -> Iterator[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        log.warning("learning: read %s failed: %s", path, e)


def _user_text(msg: Any) -> str:
    """Pull the plain-text body from a user message in either the old
    string-content shape or the newer list-of-blocks shape."""
    if msg is None:
        return ""
    if isinstance(msg, str):
        return msg.strip()
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    t = block.get("text")
                    if isinstance(t, str):
                        parts.append(t)
                elif isinstance(block, str):
                    parts.append(block)
            return "\n".join(parts).strip()
    return ""


def _assistant_text(msg: Any) -> str:
    if msg is None:
        return ""
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    t = block.get("text")
                    if isinstance(t, str):
                        parts.append(t)
            return "\n".join(parts)
    return ""


def scan_session_logs(
    *,
    projects_dir: Path | None = None,
    sessions: list[Path] | None = None,
) -> list[FeedbackEvent]:
    """Walk session JSONL logs and return detected FeedbackEvents.

    `sessions` overrides path discovery — useful for tests. Otherwise
    walks all `*.jsonl` files under `~/.claude/projects/`.
    """
    projects_dir = projects_dir or _claude_projects_dir()

    if sessions is None:
        if not projects_dir.exists():
            log.info("learning: %s does not exist; nothing to scan", projects_dir)
            return []
        sessions = []
        for sub in projects_dir.iterdir():
            if sub.is_dir():
                sessions.extend(sub.glob("*.jsonl"))
            elif sub.suffix == ".jsonl":
                sessions.append(sub)

    out: list[FeedbackEvent] = []
    for sess in sessions:
        last_assistant_text = ""
        for rec in _iter_jsonl_records(sess):
            rtype = rec.get("type")
            if rtype == "assistant":
                msg = rec.get("message")
                last_assistant_text = _assistant_text(msg)
                continue
            if rtype != "user":
                continue
            msg = rec.get("message")
            text = _user_text(msg)
            if not text or not looks_like_followup(text):
                continue
            ts = rec.get("timestamp") or datetime.now(timezone.utc).isoformat()
            artifact_path, artifact_kind = classify_event(text, last_assistant_text)
            out.append(FeedbackEvent(
                timestamp=str(ts),
                text=text,
                source="scan",
                session_id=str(rec.get("sessionId") or sess.stem),
                prior_artifact=artifact_path,
                prior_artifact_kind=artifact_kind,
            ))

    return out
