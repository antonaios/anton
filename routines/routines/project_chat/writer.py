"""Atomic append-only writer for ``Projects/<DEAL>/_chat.md``.

Each turn is appended to the deal's chat log. The on-disk format (plan §6.6)
is YAML frontmatter + a per-turn append-only markdown body. We ALSO persist
the full structured turn list as a ``data:`` JSON scalar in the frontmatter
(mirroring daily-digest) so ``reader.load_history`` round-trips exactly.

Write strategy: load the existing turns, append the new ones, re-render the
whole document, and write atomically (tempfile + rename via the shared
``vault_writer.atomic_write``). The body stays strictly append-ordered; only
the frontmatter (``last-turn`` / ``turns`` + the JSON payload) is rebuilt.

Idempotency (#42 test contract): appending a turn that already sits at the
tail of the log (same timestamp + role + text) is a no-op — re-firing the
same chat turn twice never doubles it. This makes a retried POST safe.

Sensitivity is stamped into the frontmatter so the file self-declares its
tier the moment it lands inside the project folder (where §4 rules apply).
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import threading
from pathlib import Path

import frontmatter

from routines.project_chat.reader import chat_path, load_history
from routines.project_chat.schema import ChatTurn
from routines.shared.vault_writer import atomic_write

log = logging.getLogger(__name__)


# Per-path locks serialise the whole read→render→write so two overlapping POSTs
# can't both read the same tail and clobber each other's turn (Codex SEV-1).
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCKS_GUARD:
        lk = _LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _LOCKS[key] = lk
        return lk


class ChatLogCorruptError(RuntimeError):
    """An existing ``_chat.md`` is present + non-empty but is NOT a recognisable
    project-chat log. We back it up and refuse to overwrite — failing CLOSED
    rather than silently restarting the conversation (Codex pass-2 SEV-2)."""


def _is_safe_empty_chat(raw: str) -> bool:
    """True ONLY for a PROVABLY-empty project-chat log — zero turns, so appending
    (which rewrites the whole doc) loses nothing. Anything that COULD still hold
    turns we failed to parse — a non-zero/absent ``turns`` count, an unparseable
    or non-empty ``data`` block, or body turn-headers — returns False so the
    caller FAILS CLOSED rather than overwrite real content (Codex pass-3 SEV-1)."""
    try:
        post = frontmatter.loads(raw)
    except Exception:  # noqa: BLE001 — unparseable frontmatter ⇒ not our doc
        return False
    md = post.metadata
    if md.get("type") != "project-chat":
        return False
    # Must EXPLICITLY declare zero turns. int(...) would coerce 0.5 / False to 0,
    # so accept ONLY an exact int 0 (NOT bool) or the string "0" (Codex pass-4 SEV-1).
    turns = md.get("turns")
    if not ((type(turns) is int and turns == 0) or turns == "0"):
        return False
    # A ``data`` payload, if present, must be EXACTLY {"turns": []}. Inspect the
    # raw structure ourselves — do NOT reuse the reader's forgiving parser, which
    # SKIPS malformed rows down to [] and would hide real turns (Codex pass-5 SEV-1).
    data = md.get("data")
    if data is not None:
        try:
            payload = json.loads(data) if isinstance(data, str) else data
        except (json.JSONDecodeError, TypeError):
            return False
        if not isinstance(payload, dict) or payload.get("turns") != []:
            return False
    # Body must be empty or ONLY the writer's generated title line. Any other
    # non-whitespace content — prose, a transcript, ``##``/``###`` headings — fails
    # closed rather than risk overwriting real content (Codex pass-5 SEV-1).
    body = post.content.strip()
    if body and not re.fullmatch(r"#\s+Project chat\b.*", body):
        return False
    return True


def _next_backup_path(path: Path) -> Path:
    n = 0
    while True:
        suffix = ".corrupt" if n == 0 else f".corrupt-{n}"
        cand = path.with_name(path.name + suffix)
        if not cand.exists():
            return cand
        n += 1


def _guard_existing_log(path: Path) -> None:
    """Called when an existing file parsed to ZERO turns. A recognisable
    (possibly empty) chat log returns — safe to append. A present, non-empty,
    UNRECOGNISABLE (or unreadable) file is backed up to a ``.corrupt`` sidecar
    and we FAIL CLOSED — never overwrite real-but-unparseable content with a
    fresh log, and never silently restart the conversation (Codex pass-2)."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
        readable = True
    except OSError:
        raw, readable = "", False
    if readable and not raw.strip():
        return  # genuinely empty file — safe to (re)write
    if readable and _is_safe_empty_chat(raw):
        return  # provably-empty chat log — zero turns to lose, safe to append
    # Present + non-empty + unrecognisable (or unreadable) → corrupt. Preserve + fail.
    backup = _next_backup_path(path)
    try:
        shutil.copy2(path, backup)
    except OSError as e:
        raise ChatLogCorruptError(
            f"{path} is unreadable and could not be backed up — refusing to "
            "overwrite (chat is fail-closed until the file is repaired/removed)."
        ) from e
    log.error(
        "project-chat: %s is not a recognisable chat log — backed up to %s; "
        "refusing to overwrite (fail closed)", path, backup,
    )
    raise ChatLogCorruptError(
        f"{path} is not a recognisable chat log; the original was backed up to "
        f"{backup}. Repair or remove it to resume chat."
    )


def append_turns(
    vault_root: Path,
    project: str,
    new_turns: list[ChatTurn],
    *,
    sensitivity: str = "confidential",
) -> Path:
    """Atomic-append ``new_turns`` to the deal's ``_chat.md``.

    Idempotent at the tail: any leading new turns that exactly match the
    current tail of the log (timestamp + role + text) are dropped before
    the append, so re-firing an identical turn is a no-op.

    Returns the path written. ``sensitivity`` is stamped into the frontmatter
    (the endpoint passes the deal's resolved tier).
    """
    path = chat_path(vault_root, project)
    # Serialise the whole read→dedupe→render→write under a per-path lock so
    # concurrent POSTs can't both read the same tail and drop a turn (SEV-1).
    with _lock_for(path):
        existing = load_history(vault_root, project)
        # Never truncate a present-but-unparseable log: a recognisable (even
        # empty) chat doc is safe; anything else is backed up + fails closed.
        if not existing and path.exists():
            _guard_existing_log(path)

        to_add = _dedupe_against_tail(existing, new_turns)
        if not to_add and existing and path.exists():
            # Nothing new to write and the file already exists — pure no-op.
            return path

        all_turns = existing + to_add
        md = _render_markdown(project, all_turns, sensitivity=sensitivity)
        atomic_write(path, md, vault_root=vault_root)
        log.debug("project-chat: wrote %d turn(s) to %s (+%d new)", len(all_turns), path, len(to_add))
        return path


def append_turn(
    vault_root: Path,
    project: str,
    turn: ChatTurn,
    *,
    sensitivity: str = "confidential",
) -> Path:
    """Convenience single-turn wrapper around :func:`append_turns`."""
    return append_turns(vault_root, project, [turn], sensitivity=sensitivity)


def _turn_key(t: ChatTurn) -> tuple[str, str, str]:
    """Identity tuple for idempotency: (timestamp, role, text)."""
    return (t.timestamp, t.role, t.text)


def _dedupe_against_tail(
    existing: list[ChatTurn], new_turns: list[ChatTurn],
) -> list[ChatTurn]:
    """Drop any leading ``new_turns`` that already match the existing tail.

    Walks both lists from the boundary: if the first new turn equals the last
    existing turn (and so on), those are duplicates of a prior write. Only the
    genuinely-new suffix is returned.
    """
    if not existing or not new_turns:
        return list(new_turns)
    # How many leading new turns coincide with the existing tail.
    overlap = 0
    max_overlap = min(len(existing), len(new_turns))
    for k in range(1, max_overlap + 1):
        if [_turn_key(t) for t in existing[-k:]] == [_turn_key(t) for t in new_turns[:k]]:
            overlap = k
    return list(new_turns[overlap:])


def _render_markdown(project: str, turns: list[ChatTurn], *, sensitivity: str) -> str:
    """Render the full ``_chat.md`` document: frontmatter + append-only body."""
    last_turn = turns[-1].timestamp if turns else ""
    payload = {"turns": [t.model_dump() for t in turns]}
    payload_json = json.dumps(payload, indent=2, ensure_ascii=False)

    fm = (
        "---\n"
        "type: project-chat\n"
        "memory_kind: episodic\n"
        f'project: "[[Projects/{project}]]"\n'
        f"sensitivity: {sensitivity}\n"
        f'last-turn: "{last_turn}"\n'
        f"turns: {len(turns)}\n"
        "tags: [chat, project, episodic-memory]\n"
        "data: |\n"
        + _indent(payload_json, "  ")
        + "\n---\n\n"
    )

    body_lines: list[str] = [f"# Project chat — {project}", ""]
    for t in turns:
        body_lines.append(f"## {t.timestamp} · {t.role}")
        if t.text:
            body_lines.append(t.text)
        if t.sources:
            body_lines.append("")
            body_lines.append("Sources:")
            for s in t.sources:
                body_lines.append(f"- [[{s.path}]] (score {s.score:.2f})")
        body_lines.append("")

    return fm + "\n".join(body_lines).rstrip() + "\n"


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())
