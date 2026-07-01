"""Synthesise the digest's rows + Anton's reflective close.

Pure deterministic shaping of `DigestContext` into `DigestRow`s for the
two list sections; one LLM call (local Ollama qwen3:14b) for the
reflective close paragraph.

The LLM gets: today's activity summary, top vault writes, profile
context. It returns a 2-4 sentence paragraph in en-GB, no markdown.
Fallback to a deterministic one-liner if the LLM is unreachable.
"""

from __future__ import annotations

import logging
from datetime import date

from routines.daily_digest.pull import DigestContext, RoutineActivity, VaultWrite
from routines.daily_digest.schema import DigestRow
from routines.shared.ollama_client import OllamaClient, OllamaError

log = logging.getLogger(__name__)


DEFAULT_MODEL = "qwen3:14b"


# ── Deterministic row builders ────────────────────────────────────────────


def routine_rows(routines: list[RoutineActivity]) -> list[DigestRow]:
    out: list[DigestRow] = []
    for r in routines:
        total = r.ok + r.error + r.skipped + r.partial
        if total == 0:
            continue
        sub_bits: list[str] = []
        if r.ok:
            sub_bits.append(f"{r.ok} ok")
        if r.error:
            sub_bits.append(f"{r.error} err")
        if r.skipped:
            sub_bits.append(f"{r.skipped} skipped")
        if r.partial:
            sub_bits.append(f"{r.partial} partial")
        sub = " · ".join(sub_bits)
        out.append(DigestRow(marker="routine", text=r.routine, sub=sub))
    return out


def vault_rows(writes: list[VaultWrite]) -> list[DigestRow]:
    out: list[DigestRow] = []
    for w in writes:
        time_part = w.mtime_iso[11:16] if len(w.mtime_iso) >= 16 else ""
        sub = f"{w.bucket} · {time_part} UTC" if time_part else w.bucket
        out.append(DigestRow(marker="vault", text=w.path, sub=sub))
    return out


# ── Anton's reflective close ──────────────────────────────────────────────


_CLOSE_SYSTEM = """\
You are Anton — the operator's M&A copilot, writing the end-of-day close.

Given a summary of today's activity (routines that ran, vault files touched),
write a SHORT reflective paragraph (2-4 sentences, 50-90 words) for the
operator's evening read.

Voice rules:
- en-GB spelling.
- Hedge-light. Principal-side. No corporate filler.
- No bullet lists, no markdown, no headings. Plain prose.
- Synthesise — don't restate the data. Connect dots: which routine
  produced the most signal, what's the implication for tomorrow.
- If the day was thin, say so plainly. Don't pad.

Return PLAIN TEXT only. No JSON. No markdown fences.
"""


def anton_closes(
    ctx: DigestContext,
    *,
    profile_context: str = "",
    client: OllamaClient,
    model: str = DEFAULT_MODEL,
) -> str:
    """Draft the reflective end-of-day paragraph."""
    routines_block = "\n".join(
        f"- {r.routine}: {r.ok} ok"
        + (f", {r.error} err" if r.error else "")
        + (f", {r.skipped} skipped" if r.skipped else "")
        + (f", {r.partial} partial" if r.partial else "")
        for r in ctx.routines
    ) or "(no routines ran)"

    writes_block = "\n".join(
        f"- {w.path}" for w in ctx.vault_writes[:10]
    ) or "(no vault writes)"

    user = (
        f"Today is {ctx.today.isoformat()}.\n\n"
        f"Routines that ran today:\n{routines_block}\n\n"
        f"Top vault files touched today:\n{writes_block}\n\n"
        f"Operator profile context:\n{profile_context or '(none)'}\n\n"
        f"Write Anton's end-of-day close paragraph."
    )

    try:
        resp = client.chat(model=model, prompt=user, system=_CLOSE_SYSTEM)
    except OllamaError as e:
        log.warning("daily-digest: anton_closes LLM call failed: %s", e)
        return _fallback_close(ctx)
    text = (resp.content or "").strip()
    return text or _fallback_close(ctx)


def _fallback_close(ctx: DigestContext) -> str:
    """Deterministic fallback when the LLM is unreachable."""
    n_routines = sum(r.ok + r.error + r.skipped + r.partial for r in ctx.routines)
    n_writes = len(ctx.vault_writes)
    errors = sum(r.error for r in ctx.routines)
    if errors:
        return (
            f"{errors} routine error{'s' if errors != 1 else ''} today across "
            f"{n_routines} runs. Worth a glance at the audit log before tomorrow."
        )
    if n_routines == 0 and n_writes == 0:
        return "Quiet day. Nothing material captured."
    return (
        f"{n_routines} routine run{'s' if n_routines != 1 else ''} and "
        f"{n_writes} vault write{'s' if n_writes != 1 else ''} today."
    )


# Suppress unused-import warning
_ = date
