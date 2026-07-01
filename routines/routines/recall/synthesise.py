"""Map-reduce synthesis over retrieved notes.

For each NoteHit:
    map step: ask qwen3:14b "given this query, what's relevant in this note?"
    -> short relevance summary (or "not relevant")

Then:
    reduce step: ask qwen3:14b to synthesise across the per-note summaries,
    citing source notes as `[[path]]` wikilinks. Source headers carry
    STALE / CONTRADICTED flags (from the #54-rrf expires decay + the
    #54-contradiction penalty) and the answer must close with a
    `**Gaps:**` section — what the vault does NOT cover, plus any flagged
    sources relied on (gbrain-pattern gap analysis, 2026-06-10 eval).

Designed to run end-to-end locally — no cloud lane involved, so confidential
content is safe regardless of plan tier. Per CLAUDE.md §4 + §5.

The map step reads the FULL file from disk (not just the indexed body_excerpt)
so that long documents are evaluated on their full content, not just the
first 8k chars used for the embedding.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from routines.recall.retrieve import NoteHit
from routines.shared.ollama_client import OllamaClient

logger = logging.getLogger(__name__)


MAP_SYSTEM = """\
You are extracting relevant context from a single vault note in response to a
user query. Be concise. If the note is genuinely relevant, summarise the
relevant facts in 1-3 bullets, in the operator's voice (precise, hedge-light,
M&A-literate). Cite specifics (numbers, dates, names) where the note has them.

If the note is NOT relevant to the query, reply with exactly: NOT_RELEVANT
"""


REDUCE_SYSTEM = """\
You are synthesising an answer to a user's query, drawing on excerpts from
multiple vault notes. Each excerpt is labelled with its vault path.

Rules:
- Cite each source as a wikilink: `[[<path-without-md>]]`
- Distinguish what is in the vault from what would need further research
- Do not invent specifics
- If the available excerpts do not adequately answer the query, say so explicitly
- Use British English; M&A-literate register; no fluff
- For numerical claims, preserve currency and period from the source
- Some source labels carry flags: STALE means the note is past its
  declared expiry date; CONTRADICTED means a newer vault note disagrees
  with this note's claim. Treat flagged material with caution and never
  present it as current without saying so.
- End with a short `**Gaps:**` section that states honestly: (1) aspects
  of the query the vault does not cover, (2) any cited source flagged
  STALE, (3) any cited source flagged CONTRADICTED. If none apply, end
  with exactly: `**Gaps:** none noted.`
"""


def _semantic_ok(h: NoteHit, relevance_threshold: float) -> bool:
    """Keep a hit above the SEMANTIC floor. The floor is the raw vector cosine
    (``vector_score`` ∈ [0, 1]), NOT the RRF ``score`` (which is ~0.01–0.06 under
    #54-rrf and would reject everything). A hit with NO vector component
    (``vector_score is None`` — a pure FTS5 match) is kept unconditionally: an
    exact-token match cleared the FTS lane, which is relevance signal enough
    (Codex SEV-2 — never drop FTS-only hits on the cosine floor)."""
    if h.vector_score is not None:
        return h.vector_score >= relevance_threshold
    return True


def synthesise(
    query_text: str,
    hits: list[NoteHit],
    *,
    client: OllamaClient,
    vault_root: Path,
    map_model: str = "qwen3:14b",
    reduce_model: str = "qwen3:14b",
    relevance_threshold: float = 0.4,
    max_body_chars: int = 12000,
) -> str:
    """Run the map-reduce. Returns the final synthesised answer string.

    `relevance_threshold`: drop hits below this SEMANTIC floor — below it,
    even the model's relevance check is wasteful. The floor is applied to
    the raw vector cosine (``vector_score``, still in [0, 1]), NOT the
    final ``score``: under #54-rrf the final score is a Reciprocal-Rank-
    Fusion value (~0.01–0.06 magnitude), so the old 0.4 cosine floor would
    reject every hit if read off ``score``. Reading ``vector_score``
    preserves the threshold's original cosine meaning. Hits with no vector
    component (FTS-only, ``vector_score`` is None) fall back to ``score``
    being ≥ threshold OR a non-None ``vector_score`` — i.e. FTS-only hits
    are kept (a strong exact-token match is its own relevance signal).
    """
    survivors = [h for h in hits if _semantic_ok(h, relevance_threshold)]
    if not survivors:
        return (
            "No vault notes matched the query above the relevance threshold "
            f"(>= {relevance_threshold}). Either the vault doesn't contain "
            "relevant material yet, or the query needs reformulation."
        )

    # ---- map step: per-note relevance summaries
    # Read the FULL file from disk for the map step (not just the index excerpt)
    # so long documents are evaluated on full content.
    map_outputs: list[tuple[str, str, NoteHit]] = []  # (path, summary, hit)
    for h in survivors:
        full_path = vault_root / h.path
        try:
            full_text = full_path.read_text(encoding="utf-8", errors="replace")
            # Cap to keep within model context — qwen3:14b can handle ~32k tokens
            # but quality drops on very long inputs
            if len(full_text) > max_body_chars:
                full_text = full_text[:max_body_chars] + "\n\n[... truncated ...]"
        except (FileNotFoundError, OSError):
            # File deleted between index time and now — skip
            logger.warning("file gone since index: %s", h.path)
            continue

        cosine = h.vector_score if h.vector_score is not None else h.score
        prompt = (
            f"User query:\n{query_text}\n\n"
            f"Vault note path: {h.path}\n"
            f"Cosine similarity: {cosine:.3f}\n\n"
            f"---\n{full_text}\n---\n\n"
            "What from this note is relevant to the query? "
            "Reply NOT_RELEVANT if it isn't."
        )
        try:
            resp = client.chat(
                model=map_model, prompt=prompt, system=MAP_SYSTEM,
                temperature=0.2, max_tokens=300,
            )
            content = resp.content.strip()
            if content.upper().startswith("NOT_RELEVANT"):
                continue
            if content:
                map_outputs.append((h.path, content, h))
        except Exception as e:  # noqa: BLE001
            logger.warning("map step failed for %s: %s", h.path, e)
            continue

    if not map_outputs:
        return (
            "Found candidate notes by semantic search, but none were assessed "
            "as actually relevant in the per-note relevance check. The vault "
            "likely doesn't yet contain material that answers the query."
        )

    # ---- reduce step: synthesise across the surviving excerpts
    #
    # Gap analysis (gbrain-pattern adoption — 2026-06-10 eval): each source
    # header carries the retrieval layer's freshness verdicts so the reduce
    # model can weigh + disclose them. STALE = the note is past its
    # frontmatter ``expires`` date (``expires_decay`` < 1.0 under #54-rrf);
    # CONTRADICTED = #54-contradiction demoted this note as the OLDER of two
    # disagreeing same-(subject, field) claims (``contradiction_penalty``
    # < 1.0). The REDUCE_SYSTEM contract then requires a closing
    # ``**Gaps:**`` section — uncovered aspects + flagged sources — so the
    # synthesis states what it does NOT know rather than implying coverage.
    def _flags(h: NoteHit) -> str:
        parts: list[str] = []
        if h.expires_decay is not None and h.expires_decay < 1.0:
            parts.append("STALE — past its declared expiry date")
        if h.contradiction_penalty is not None and h.contradiction_penalty < 1.0:
            parts.append("CONTRADICTED — a newer vault note disagrees")
        return f"  ⚑ {'; '.join(parts)}" if parts else ""

    flagged: list[tuple[str, str]] = [
        (path, _flags(h).lstrip())
        for path, _summary, h in map_outputs
        if _flags(h)
    ]
    reduce_input = "\n\n".join(
        f"### Source: [[{path.removesuffix('.md')}]]{_flags(hit)}\n{summary}"
        for path, summary, hit in map_outputs
    )
    reduce_prompt = (
        f"User query:\n{query_text}\n\n"
        "Per-note relevance summaries (each from a different vault note):\n\n"
        f"{reduce_input}\n\n"
        "Synthesise an answer to the query above using only what's in these "
        "summaries. Cite each source as a wikilink in the body of your answer. "
        "Be concise and direct. If the summaries don't answer the query "
        "adequately, say so — do not pad. Finish with the required "
        "`**Gaps:**` section."
    )

    try:
        resp = client.chat(
            model=reduce_model, prompt=reduce_prompt, system=REDUCE_SYSTEM,
            temperature=0.2,
        )
        answer = resp.content.strip()
        # The Gaps section is a CONTRACT, not a hope: a local reduce model
        # can omit a prompt-only requirement, so validate and append a
        # deterministic fallback built from the retrieval-layer flags
        # (codex SEV-2 R1 + SEV-3 R2, 2026-06-10 review).
        # Deterministic-append over retry-with-repair: a second qwen3:14b
        # round adds 30s+ latency for a section we can already state
        # exactly. Detection is an ANCHORED heading match (a line starting
        # with "Gaps:" / "**Gaps:**"), not a substring — mid-prose
        # "coverage gaps: …" must not satisfy the contract (codex R2).
        flag_list = "; ".join(
            f"[[{p.removesuffix('.md')}]] ({f.lstrip('⚑ ')})"
            for p, f in flagged
        )
        if not re.search(r"(?im)^\s*(?:\*\*)?gaps:", answer):
            if flagged:
                answer += (
                    "\n\n**Gaps:** flagged sources relied on — " + flag_list
                )
            else:
                answer += "\n\n**Gaps:** none noted."
        elif flagged:
            # A Gaps heading exists, but flagged-source disclosure must not
            # stay prompt-only (codex R2): if any flag word the input
            # carried (STALE / CONTRADICTED) is absent from the answer, the
            # model glossed over it — append the deterministic disclosure.
            needed = {
                w for _p, f in flagged
                for w in ("STALE", "CONTRADICTED") if w in f
            }
            if any(w not in answer for w in needed):
                answer += (
                    "\n\n**Flagged sources relied on (disclosure):** "
                    + flag_list
                )
        return answer
    except Exception as e:  # noqa: BLE001
        logger.exception("reduce step failed: %s", e)
        # Fallback: return the per-note summaries verbatim
        return "Synthesis failed; per-note relevance summaries:\n\n" + reduce_input
