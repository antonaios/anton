"""Read a deal's ``_chat.md`` history into ``ChatTurn`` objects.

The on-disk format (plan §6.6) is YAML frontmatter + an append-only body of
per-turn markdown blocks:

    ## <ISO-timestamp> · <role>
    <turn text, possibly multi-line>

    Sources:
    - [[<vault-relative-path>]] (score 0.84)
    ...

The full structured payload is also stored as a ``data:`` JSON scalar block
in the frontmatter (same trick daily-digest uses) so the reader round-trips
exactly without re-parsing prose. We prefer the JSON payload when present and
fall back to parsing the markdown body for hand-edited / legacy files.

Used by ``GET /api/projects/{code}/chat/history`` (read-only) and by
``pull.load_history`` (which truncates to the last N turns for the LLM window).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

import frontmatter

from routines.project_chat.schema import ChatSource, ChatTurn

log = logging.getLogger(__name__)


def chat_path(vault_root: Path, project: str) -> Path:
    """Resolve ``Projects/<project>/_chat.md`` under the vault root."""
    return vault_root / "Projects" / project / "_chat.md"


def load_history(vault_root: Path, project: str) -> list[ChatTurn]:
    """Load all turns for ``project`` in chronological order.

    Returns ``[]`` when the file is absent / unreadable / malformed — chat
    history is best-effort context, never load-bearing, so a broken file
    degrades to "empty history" rather than raising.
    """
    path = chat_path(vault_root, project)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning("project-chat: read %s failed: %s", path, e)
        return []
    return _parse_turns(text)


def _parse_turns(text: str) -> list[ChatTurn]:
    """Parse a ``_chat.md`` document into ``ChatTurn`` objects.

    Prefers the structured ``data:`` JSON payload in the frontmatter; falls
    back to parsing the markdown body when it's absent (hand-edited files).
    """
    try:
        post = frontmatter.loads(text)
    except Exception as e:  # noqa: BLE001 — never crash on a YAML wart
        log.warning("project-chat: frontmatter parse failed: %s", e)
        return _parse_body_turns(text)

    raw = post.metadata.get("data")
    if raw:
        turns = _turns_from_payload(raw)
        if turns is not None:
            return turns
    return _parse_body_turns(post.content)


def _turns_from_payload(raw: object) -> list[ChatTurn] | None:
    """Build turns from the frontmatter ``data:`` JSON scalar. Returns
    ``None`` (signalling "fall back to body parse") on any parse failure."""
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as e:
        log.warning("project-chat: data payload JSON parse failed: %s", e)
        return None
    if not isinstance(payload, dict):
        return None
    rows = payload.get("turns")
    if not isinstance(rows, list):
        return None
    out: list[ChatTurn] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            out.append(ChatTurn(
                timestamp=str(r.get("timestamp", "")),
                role=r.get("role", "user"),
                text=str(r.get("text", "")),
                sources=[
                    ChatSource(
                        path=str(s.get("path", "")),
                        score=float(s.get("score", 0.0)),
                        excerpt=str(s.get("excerpt", "")),
                    )
                    for s in (r.get("sources") or [])
                    if isinstance(s, dict)
                ],
            ))
        except Exception as e:  # noqa: BLE001 — skip a bad row, keep the rest
            log.warning("project-chat: skipping malformed turn row: %s", e)
            continue
    return out


# Header line: "## 2026-05-15T14:23:08+00:00 · user"
_TURN_HEADER_RE = re.compile(
    r"^##\s+(?P<ts>\S+)\s+·\s+(?P<role>user|assistant)\s*$",
    re.MULTILINE,
)
# A source line in the body: "- [[Projects/FALCON/05 Research/x.md]] (score 0.84)"
_SOURCE_RE = re.compile(
    r"^-\s+\[\[(?P<path>[^\]]+)\]\](?:\s+\(score\s+(?P<score>[0-9.]+)\))?\s*$"
)


def _parse_body_turns(body: str) -> list[ChatTurn]:
    """Fallback markdown-body parser for hand-edited / legacy ``_chat.md``.

    Splits on ``## <ts> · <role>`` headers; everything up to the next header
    (or a ``Sources:`` line) is the turn text; ``- [[path]] (score N)`` lines
    after a ``Sources:`` marker become ``ChatSource`` records.
    """
    matches = list(_TURN_HEADER_RE.finditer(body))
    out: list[ChatTurn] = []
    for i, m in enumerate(matches):
        ts = m.group("ts").strip()
        role = m.group("role").strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        block = body[start:end]

        text_lines: list[str] = []
        sources: list[ChatSource] = []
        in_sources = False
        for line in block.splitlines():
            stripped = line.strip()
            if stripped.lower() == "sources:":
                in_sources = True
                continue
            if in_sources:
                sm = _SOURCE_RE.match(stripped)
                if sm:
                    sources.append(ChatSource(
                        path=sm.group("path").strip(),
                        score=float(sm.group("score")) if sm.group("score") else 0.0,
                        excerpt="",
                    ))
                continue
            text_lines.append(line)

        text = "\n".join(text_lines).strip()
        out.append(ChatTurn(
            timestamp=ts,
            role=role,  # type: ignore[arg-type]
            text=text,
            sources=sources,
        ))
    return out
