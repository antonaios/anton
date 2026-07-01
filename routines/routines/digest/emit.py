"""Stage 5 — emit the digest to the vault (#ingest-digest). BRIDGE-SIDE.

The crew (3.11 venv, forbidden ``import routines.*``) CANNOT write the vault —
the central write chokepoint (``routines.shared.write_policy`` / ``vault_writer``)
is bridge-side. So stage 5 runs HERE: the crew returns the stage-1-4
``DigestSliceResult`` over stdio (persisted as the intermediate ``.digest.json``),
and this module renders + writes ONE aggregate digest note to
``Projects/<deal>/digest/`` (operator decision 1) via ``vault_writer.atomic_write``
(→ ``ensure_write_allowed``; ``Projects/**`` is an allowed write prefix). It
operates on the parsed intermediate DICT — not the crew-side pydantic model —
so it needs no cross-venv import (same as the CLI's ``_print_slice``).

OPERATOR-GATED (operator decision, 2026-06-15): DRY-RUN by default —
:func:`emit_digest` renders the note and returns the would-write path + content
WITHOUT touching the vault; the actual write happens only when the caller passes
``write=True`` (the CLI ``--emit`` flag / a future route arg). CREATE-ONLY: a
note is NEVER overwritten ([no-overwrite-without-confirmation]) — the run-id in
the filename makes a real collision near-impossible, and a collision still gets a
numeric suffix rather than clobbering.

The note carries the frontmatter triad (importance / expires / provenance) +
``source_tier`` so recall indexes it (zero new query infra). NOTE: this v1 emits
ONE aggregate digest note per run; feeding recall's NARROW per-claim
contradiction detector (which reads a single subject/field/value per note —
``recall/CONTRADICTION-NOTES.md``) would need per-claim notes and is a documented
follow-on, not v1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from routines.shared.vault_writer import atomic_write, serialise_note


@dataclass
class EmitResult:
    """Outcome of an emit (or dry-run). ``written`` is True only when a write
    actually happened; ``content`` is always populated (the dry-run preview)."""

    path: str            # absolute target path (the would-write path in dry-run)
    rel_path: str        # vault-relative path
    written: bool
    bytes: int
    content: str


def _runid8(run_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", run_id or "")[:8] or "run"


def _safe_component(name: str) -> str:
    """A filesystem-safe single path component for the deal name — strips
    separators + odd chars so the deal name can't inject a subpath or traversal.
    The central write policy is the real gate; this is defence in depth."""
    s = re.sub(r"[^A-Za-z0-9 ._-]", "_", (name or "").strip()) or "unknown"
    return s.strip(" .")[:80] or "unknown"


def source_tier(analyses: list[dict]) -> int:
    """Coarse #54 source-tier for the AGGREGATE note: tier 1 (most authoritative)
    ONLY when there are contributing docs AND EVERY one is a verified-public
    filing/transcript; otherwise tier 2 (internal/confidential). A mixed pile is
    labelled by its MORE-restrictive tier, so a single confidential CIM is never
    mislabelled filing-grade (codex review). Tier 3 (scraped) doesn't apply to a
    drop-dir pile; per-fact provenance is in the body too."""
    docs = analyses or []
    if docs and all((a.get("routing") or {}).get("verified_public") for a in docs):
        return 1
    return 2


def _fact_line(f: dict) -> str:
    seg = f"{f.get('field')} = {f.get('value')}"
    extra = " ".join(x for x in (f.get("unit"), f.get("period")) if x)
    if extra:
        seg += f" ({extra})"
    prov = str(f.get("provenance") or "").strip()
    if prov:
        seg += f"  — {prov}"
    return seg


def render_digest_note(digest: dict, *, run_id: str, created: str) -> tuple[str, str]:
    """Render ``(vault_relative_path, markdown)`` for the digest note. PURE — no
    I/O — so it is unit-testable and the caller decides whether to write it."""
    deal = str(digest.get("project") or "unknown")
    syn = digest.get("synthesis") or {}
    rev = digest.get("review") or {}
    analyses = digest.get("analyses") or []

    rel_path = (
        f"Projects/{_safe_component(deal)}/digest/"
        f"{_safe_component(created)}-digest-{_runid8(run_id)}.md"
    )

    meta = {
        "kind": "digest",
        "project": deal,
        "created": created,
        "importance": "normal",
        "expires": "",                      # deal facts don't auto-expire in v1
        "provenance": f"digest:{run_id}",   # #54a provenance source
        "source_tier": source_tier(analyses),
    }

    lines: list[str] = [f"# Digest — {deal} ({created})", ""]
    narrative = str(syn.get("narrative") or "").strip()
    if narrative:
        lines += [narrative, ""]

    entities = syn.get("entities") or []
    if entities:
        lines.append("## Entities")
        for e in entities:
            wl = e.get("wikilink") or f"[[{e.get('name')}]]"
            lines.append(f"- {wl} ({e.get('mentions', 0)} doc(s))")
        lines.append("")

    facts = syn.get("facts") or []
    if facts:
        lines.append("## Facts")
        order: list[str] = []
        by_subj: dict[str, list[dict]] = {}
        for f in facts:
            s = str(f.get("subject") or "?")
            if s not in by_subj:
                by_subj[s] = []
                order.append(s)
            by_subj[s].append(f)
        for s in order:
            lines.append(f"### {s}")
            lines += [f"- {_fact_line(f)}" for f in by_subj[s]]
            lines.append("")

    cons = syn.get("contradictions") or []
    if cons:
        lines.append("## Contradictions")
        for c in cons:
            vals = " vs ".join(str(e.get("value")) for e in (c.get("entries") or []))
            lines.append(f"- **{c.get('subject')} | {c.get('field')}**: {vals}")
        lines.append("")

    if rev:
        gate = "passed" if rev.get("passed") else "FAILED"
        lines.append("## Review")
        lines.append(f"- gate: **{gate}**")
        uncited = rev.get("uncited") or []
        if uncited:
            lines.append(f"- uncited (would be withheld at write): {len(uncited)}")
            lines += [f"  - {u}" for u in uncited[:50]]
        if rev.get("new_entities"):
            lines.append(f"- new entities: {', '.join(rev['new_entities'][:50])}")
        if rev.get("orphan_subjects"):
            lines.append(f"- orphan subjects: {', '.join(rev['orphan_subjects'][:50])}")
        lines.append("")

    body = "\n".join(lines).rstrip() + "\n"
    return rel_path, serialise_note(meta, body)


def _unique_target(target: Path) -> Path:
    """Never overwrite ([no-overwrite-without-confirmation]): if ``target``
    exists, append ``-2``, ``-3``, … before the suffix until a free path. The
    run-id in the filename already makes a real collision near-impossible.

    NB check-then-write (TOCTOU): a STRICT create-only guarantee under concurrent
    writers would need an O_EXCL primitive at the ``vault_writer`` chokepoint
    (shared-infra, out of scope here) — for the digest's one-note-per-unique-
    run-id case the race window isn't reachable in practice."""
    if not target.exists():
        return target
    parent, stem, suffix = target.parent, target.stem, target.suffix
    i = 2
    while True:
        cand = parent / f"{stem}-{i}{suffix}"
        if not cand.exists():
            return cand
        i += 1


def emit_digest(
    digest: dict,
    *,
    run_id: str,
    created: str,
    vault_root: Path,
    write: bool = False,
) -> EmitResult:
    """Render the digest note and, ONLY when ``write=True``, ``atomic_write`` it
    to the vault (via ``ensure_write_allowed``; create-only, never overwrite).
    DRY-RUN by default — returns the would-write path + content with no side
    effect. ``vault_root`` MUST be server-config-derived (``deps.VAULT`` /
    ``VaultPaths.root``), never request input — it is the write-policy anchor."""
    rel_path, markdown = render_digest_note(digest, run_id=run_id, created=created)
    nbytes = len(markdown.encode("utf-8"))
    root = Path(vault_root)
    target = root / rel_path
    if not write:
        return EmitResult(path=str(target), rel_path=rel_path, written=False,
                          bytes=nbytes, content=markdown)
    target = _unique_target(target)
    atomic_write(target, markdown, vault_root=root)
    # rel_path may have gained a -N collision suffix — report what was WRITTEN.
    rel_path = target.relative_to(root).as_posix()
    return EmitResult(path=str(target), rel_path=rel_path, written=True,
                      bytes=nbytes, content=markdown)


__all__ = ["EmitResult", "source_tier", "render_digest_note", "emit_digest"]
