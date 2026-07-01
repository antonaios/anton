"""Deliverable→vault capture (#76; generalised to crews via
#captures-to-vault-crews) — emit an operator-gated proposal carrying a
skill's OR crew's CONCLUSION back toward the vault's semantic memory.

The gap this closes (operator Q7, 2026-05-29): deliverable-producing skills
write the artefact to the workspace (an ``/lbo`` run lands a populated XLSX in
the deal's Valuation folder) but the vault never learns the *conclusion*
("DemoDeal screened at ~10% IRR / 1.6x at 10x entry, 2026-05-29"), so a later
``/recall "what did we value DemoDeal at?"`` returns nothing.

This module is the bridge. After a successful run of a skill whose SKILL.md
declares a ``captures_to_vault:`` block (see
:class:`routines.skills.registry.CapturesToVault`), the skill's route handler
calls :func:`emit_deliverable_proposal`. It renders the declared headline +
fields from the skill's structured result and writes a
``kind: deliverable-outcome`` proposal to ``Routines/deliverable-outcomes/``.

It does **NOT** write to the vault's semantic layer. The proposal is
operator-gated: it surfaces in ``GET /api/proposals/pending`` (approval tier),
and only on operator **Route** does the conclusion land as a dated, sourced
fact on the target note (see ``routines.api.routes.proposals`` —
``_route_deliverable_outcome``). This is the whole design — every capture is
gated, consistent with CLAUDE.md §3 rule 9 (never auto-write the vault).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import frontmatter

from routines.skills.registry import CapturesToVault

log = logging.getLogger(__name__)


PROPOSAL_DIR_REL = "Routines/deliverable-outcomes"

# Operator-triaged statuses: an emitted proposal whose status has progressed
# past pending-review must not be stomped by a re-run (mirrors
# routines.learning.system_insights.writer.SKIP_STATUSES).
SKIP_STATUSES = {"applied", "rejected", "routed", "revision-requested"}

# Rendered placeholder for a field that's missing from the result or is None —
# graceful (no KeyError crash) per the templating contract.
_MISSING = "n/a"

# emit_deliverable_proposal accepts exactly these provenance kinds; the value
# becomes BOTH the ``runs:<kind>...`` marker AND a frontmatter source key, so an
# unexpected value would mint an arbitrary key (#captures-to-vault-crews review).
_PROVENANCE_KINDS = ("skill", "crew")


# ─────────────────────────────────────────────────────────────────────────────
# Result flattening + templating
# ─────────────────────────────────────────────────────────────────────────────


def flatten_result(result: dict[str, Any]) -> dict[str, Any]:
    """Flatten a skill's structured result into a single namespace for
    templating + field capture.

    Top-level scalars stay as-is; one level of nested dicts (LBOOutput's
    ``returns`` / ``headline`` / ``validation``) is lifted to the top WITHOUT
    overwriting an existing top-level key. ``target`` is aliased to
    ``deal_name`` so a SKILL.md template can use either ``{target}`` or
    ``{deal_name}``."""
    flat: dict[str, Any] = {}
    nested: dict[str, Any] = {}
    for k, v in result.items():
        if isinstance(v, dict):
            nested[k] = v
        else:
            flat[k] = v
    for block in nested.values():
        for k, v in block.items():
            if k not in flat and not isinstance(v, (dict, list)):
                flat[k] = v
    # Convenience alias: the deliverable's subject. LBO's identifier is
    # deal_name; the brief's templates use {target}.
    if "deal_name" in flat and "target" not in flat:
        flat["target"] = flat["deal_name"]
    return flat


class _SafeDict(dict):
    """``str.format_map`` mapping whose missing keys render as ``n/a`` rather
    than raising KeyError — keeps headline templating graceful."""

    def __missing__(self, key: str) -> str:  # noqa: D401
        return _MISSING


def render_template(template: str, ctx: dict[str, Any]) -> str:
    """Render ``{field}`` placeholders against ``ctx``.

    Missing keys and ``None`` values both render as ``n/a`` (graceful — a thin
    deal brief shouldn't crash the capture). Any other formatting error (e.g. a
    stray ``{`` in the template) degrades to the raw template rather than
    raising."""
    safe = _SafeDict({k: (_MISSING if v is None else v) for k, v in ctx.items()})
    try:
        return template.format_map(safe)
    except (ValueError, IndexError) as e:  # malformed template — don't crash capture
        log.warning("capture: headline template render failed (%s): %r", e, template)
        return template


# ─────────────────────────────────────────────────────────────────────────────
# Proposal emission
# ─────────────────────────────────────────────────────────────────────────────


def _slug(text: str) -> str:
    """Filesystem-safe slug for the proposal filename (lowercase, dashed)."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", str(text).strip().lower())
    return s.strip("-") or "deal"


def proposal_path_for(
    vault_root: Path, *, name: str, ctx: dict[str, Any], now: datetime,
) -> Path:
    """``<vault>/Routines/deliverable-outcomes/<date>-<subject>-<name>.md``.

    ``name`` is the emitting skill name or crew verb. The subject slug prefers
    an explicit ``subject`` key (crews set it to their entity/deal identifier),
    then the skill conventions ``deal_name`` / ``target`` (back-compat)."""
    subject = _slug(
        ctx.get("subject") or ctx.get("deal_name") or ctx.get("target") or "deal"
    )
    filename = f"{now.date().isoformat()}-{subject}-{_slug(name)}.md"
    return vault_root / PROPOSAL_DIR_REL / filename


def _render_body(
    headline: str, *, name: str, provenance_kind: str, target: str, now: datetime,
    promoted_roles: Optional[dict[str, str]] = None,
) -> str:
    ts = now.strftime("%Y-%m-%d %H:%M %Z").strip()
    # #crew-cloud-promotion harmony: when a crew was cloud-promoted, record WHICH
    # roles routed to WHICH cloud lane so the captured conclusion is honest about
    # how it was generated (Opus vs qwen3:8b is the whole value of promotion).
    routing_note = ""
    if promoted_roles:
        roles = ", ".join(f"{r}→{lane}" for r, lane in promoted_roles.items())
        routing_note = (
            f"**Generation routing (this run):** {roles} promoted to a frontier "
            f"cloud model — this conclusion is NOT a local-Ollama output for those "
            f"roles. (A credit-exhaustion degrade would answer locally; confirm the "
            f"live lane in the run's LLM-call audit.)\n\n"
        )
    return (
        f"# {headline}\n\n"
        f"## Captured conclusion\n\n"
        f"{headline}\n\n"
        f"{routing_note}"
        f"On **Route**, this conclusion is appended as a dated, sourced fact to "
        f"`{target}` — append-only, never overwriting prior entries "
        f"(CLAUDE.md §3 rule 9).\n\n"
        f"*Emitted by the `{name}` {provenance_kind} deliverable→vault capture "
        f"loop (#76 / #captures-to-vault-crews) on {ts}. Routes through the "
        f"standard proposals lifecycle (#8 + #58): Review and Route / Reject / "
        f"Skip / Request revision.*\n"
    )


def _is_safe_capture_target(target: str) -> bool:
    """A rendered capture target must be a VAULT-RELATIVE note path with a real
    identifier. Reject: blank; an unresolved field (the ``_MISSING`` sentinel
    leaked into the path — e.g. a missing ``{entity}`` → ``Companies/n/a.md``);
    an absolute path / drive / backslash; or a ``..`` traversal segment.

    Defence-in-depth — the Route handler's ``_safe_vault_target`` is the hard
    gate (it re-confines + 422s an escaping path), but a proposal must not even
    be WRITTEN with a junk/escaping target."""
    t = (target or "").strip()
    if not t or _MISSING in t:
        return False
    if t.startswith(("/", "\\")) or "\\" in t or ":" in t:
        return False
    return ".." not in t.split("/")


def emit_deliverable_proposal(
    name: str,
    captures: CapturesToVault,
    result: dict[str, Any],
    *,
    vault_root: Path,
    sensitivity: str = "internal",
    now: Optional[datetime] = None,
    provenance_kind: str = "skill",
    artefact: Optional[str] = None,
    promoted_roles: Optional[dict[str, str]] = None,
) -> Optional[Path]:
    """Render + persist a ``deliverable-outcome`` proposal from a skill OR crew
    result.

    Returns the path written, or ``None`` if skipped (idempotency: a same-day
    same-subject same-emitter proposal the operator has already triaged is left
    untouched). Pure I/O: writes one proposal file, never touches the vault's
    semantic layer.

    ``name`` is the skill name or crew verb. ``provenance_kind`` ("skill" |
    "crew") selects the ``runs:<kind>.<name>.<run_id>`` provenance marker + the
    frontmatter source key; it defaults to "skill" so existing skill callers are
    unchanged. ``artefact`` is the workspace deliverable path to point at — when
    ``None`` (skills) it falls back to the skill's ``output_xlsx_path`` result
    key (back-compat); a crew passes its materialised memo path explicitly (or
    "" for a chat-only crew).

    The caller (the skill/crew route handler) is expected to treat a failure
    here as non-fatal — the deliverable already succeeded; a capture miss must
    not fail the run.
    """
    if provenance_kind not in _PROVENANCE_KINDS:
        raise ValueError(
            f"emit_deliverable_proposal: provenance_kind must be one of "
            f"{_PROVENANCE_KINDS}, got {provenance_kind!r}"
        )
    now = now or datetime.now(timezone.utc)
    ctx = flatten_result(result)

    target = render_template(captures.target, ctx)
    headline = render_template(captures.headline, ctx)
    # Fail-closed: never persist a proposal whose target note is blank,
    # unresolved (a missing identifier leaves the _MISSING sentinel in the
    # path), or escaping (absolute / backslash / drive / ``..``). The Route
    # handler's _safe_vault_target is the hard gate; this stops a junk/escaping
    # target from even reaching a proposal file (#captures-to-vault-crews review).
    if not _is_safe_capture_target(target):
        log.info(
            "capture: skipping %s capture — unsafe/unresolved target %r", name, target,
        )
        return None
    # Captured metrics keep their RAW values (incl. None) for the record.
    fields = {f: ctx.get(f) for f in captures.fields}
    run_id = str(ctx.get("run_id") or "")
    provenance = f"runs:{provenance_kind}.{name}.{run_id}"
    # Explicit artefact wins; else fall back to the skill XLSX path (back-compat).
    artefact_path = artefact if artefact is not None else str(ctx.get("output_xlsx_path") or "")

    path = proposal_path_for(vault_root, name=name, ctx=ctx, now=now)

    if path.is_file():
        try:
            existing = frontmatter.load(path)
            status = str(existing.metadata.get("status") or "").strip().lower()
            if status in SKIP_STATUSES:
                log.info(
                    "capture: skipping write to %s — operator-triaged status=%r",
                    path, status,
                )
                return None
        except Exception as e:  # noqa: BLE001 — unreadable existing file → overwrite
            log.warning("capture: failed to parse existing %s (%s) — overwriting", path, e)

    post = frontmatter.Post(_render_body(
        headline, name=name, provenance_kind=provenance_kind, target=target, now=now,
        promoted_roles=promoted_roles,
    ))
    post.metadata["type"] = "deliverable-outcome"
    post.metadata["kind"] = "deliverable-outcome"
    post.metadata["status"] = "pending-review"
    post.metadata["date"] = now.date().isoformat()
    # Source key named by kind: "skill": <name> (back-compat) | "crew": <verb>.
    post.metadata[provenance_kind] = name
    post.metadata["target"] = target
    post.metadata["section"] = captures.section
    post.metadata["headline"] = headline
    post.metadata["fields"] = fields
    post.metadata["provenance"] = provenance
    post.metadata["workspace_artefact"] = artefact_path
    post.metadata["run_id"] = run_id
    post.metadata["sensitivity"] = sensitivity
    post.metadata["tldr"] = headline
    # #crew-cloud-promotion harmony: durable, queryable record of the cloud
    # promotion (if any) so a captured conclusion isn't indistinguishable from a
    # local-Ollama one. Absent for un-promoted runs (skills + local crews).
    if promoted_roles:
        post.metadata["promoted_roles"] = dict(promoted_roles)

    path.parent.mkdir(parents=True, exist_ok=True)
    serialised = frontmatter.dumps(post) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(serialised, encoding="utf-8")
    tmp.replace(path)
    log.info("capture: wrote deliverable-outcome proposal %s", path)
    return path


__all__ = [
    "PROPOSAL_DIR_REL",
    "SKIP_STATUSES",
    "flatten_result",
    "render_template",
    "proposal_path_for",
    "emit_deliverable_proposal",
]
