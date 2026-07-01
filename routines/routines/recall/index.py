"""Embedding index for the vault.

SQLite-backed. Three tables co-located in `<vault>/.recall-index/index.db`:

  notes      — one row per file (frontmatter metadata + a whole-file
               embedding, used for backward compatibility and quick scans).
  chunks     — one row per ~500-word window (with 50-word overlap) of the
               body. Phase 2 retrieval queries this table so long documents
               get fairly-ranked instead of underranked by single-doc
               embedding.
  recall_fts — FTS5 virtual table over (path, title, body) + UNINDEXED
               (importance, expires_iso, provenance). Sidecar for the
               #54b hybrid recall scoring (vector × FTS5 × importance,
               with `expires` decay applied post-score). Tokenizer is
               ``unicode61 remove_diacritics 2`` for UK English + occasional
               accented names + financial-term acronyms — the default
               ``simple`` tokenizer would mangle these.
  index_runs — last ``_INDEX_RUNS_KEEP`` index-run stat rows (counts +
               error/degraded paths as JSON), read by ``recall health``
               and the vault_health sweep.

Embed failures degrade, never vanish (#recall-embed-context-overflow):
a note whose embed call fails is still written to ``notes`` (NULL
embedding) + ``recall_fts``, so /recall's FTS lane keeps finding it; the
NULL embedding queues it for re-embed on the next run. Embed inputs are
capped to the embed model's REAL context window — see ``EMBED_MAX_CHARS``.

Re-index logic: on each ``recall index`` run, walk the vault, compute
``file_hash`` for each .md file, compare to stored hash, re-embed only
changed files (both the whole-file embedding AND all of that file's
chunks). The FTS5 row is also re-written when the hash changes; in
addition, any scanned file without an FTS5 row gets one backfilled even
if the hash is unchanged, so upgrades (where notes + chunks were
populated by an older indexer) don't need an explicit ``--rebuild``.
Idempotent: every FTS5 write is DELETE-then-INSERT.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import struct
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import frontmatter

from routines.shared.ollama_client import OllamaClient, OllamaError

logger = logging.getLogger(__name__)


DEFAULT_INDEX_DIR = ".recall-index"
DEFAULT_INDEX_DB = "index.db"
DEFAULT_EMBED_MODEL = "nomic-embed-text"

# Folders to skip during indexing (gitignored or non-content)
SKIP_DIRS = {
    ".git",
    ".obsidian",
    ".smart-env",
    ".recall-index",
    "Inbox/HiNotes/incoming",  # raw transcripts before processing
    "Templates",                # template files: structural placeholders, not content
    "Projects/_template",       # template project room: structural, not real
    "Projects/_Trackers",       # tracker workbooks (xlsx); the .md inside is just a label
    # NB: we DO index Inbox/HiNotes/processed/ — the verbatim transcripts there
    # are exactly what we want to find with /recall ("where did we mention
    # working capital").
}

# Specific top-level files to skip (vault README, deployment guide, etc. —
# they're meta-documentation, not content that answers retrieval queries).
SKIP_FILES = {
    "README.md",
    "DEPLOYMENT.md",
}


# ============================================================ schema


_SCHEMA = """\
CREATE TABLE IF NOT EXISTS notes (
    path             TEXT PRIMARY KEY,
    file_hash        TEXT NOT NULL,
    frontmatter_json TEXT,
    tldr             TEXT,
    body_excerpt     TEXT,
    modified_at      TEXT,
    indexed_at       TEXT,
    embedding_model  TEXT,
    embedding        BLOB
);
CREATE INDEX IF NOT EXISTS notes_modified_idx ON notes(modified_at);

CREATE TABLE IF NOT EXISTS chunks (
    path             TEXT NOT NULL,
    chunk_idx        INTEGER NOT NULL,
    chunk_text       TEXT NOT NULL,
    char_start       INTEGER,
    char_end         INTEGER,
    file_hash        TEXT NOT NULL,
    embedding_model  TEXT,
    embedding        BLOB,
    PRIMARY KEY (path, chunk_idx)
);
CREATE INDEX IF NOT EXISTS chunks_path_idx ON chunks(path);

CREATE VIRTUAL TABLE IF NOT EXISTS recall_fts USING fts5(
    path,
    title,
    body,
    importance UNINDEXED,
    expires_iso UNINDEXED,
    provenance UNINDEXED,
    tokenize = "unicode61 remove_diacritics 2"
);

CREATE TABLE IF NOT EXISTS index_runs (
    run_at      TEXT NOT NULL,
    counts_json TEXT NOT NULL
);
"""

# How many index-run stat rows to retain in ``index_runs`` (newest kept).
_INDEX_RUNS_KEEP = 20


# Frontmatter triad helpers (#54a contract — CLAUDE.md §3 rule 12).
#
# importance: 1..5 integer, default 3 (mid-neutral) when unset/malformed.
# expires:    YYYY-MM-DD string; empty string when unset/malformed.
# provenance: free-form string (wikilink, URL, source-register anchor);
#             empty string when unset.


def parse_importance(value: Any) -> int:
    """Coerce a frontmatter ``importance`` value to int in [1, 5].

    Defaults to 3 (the operator-neutral mid-point) for unset/malformed
    values. Out-of-range integers are clamped rather than rejected — the
    intent of an operator who typed ``importance: 7`` is "very high",
    not "broken note".
    """
    if value is None or value == "":
        return 3
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 3
    if n < 1:
        return 1
    if n > 5:
        return 5
    return n


def parse_expires_iso(value: Any) -> str:
    """Coerce a frontmatter ``expires`` value to a YYYY-MM-DD string.

    Accepts ``datetime.date`` / ``datetime.datetime`` (the YAML parser
    auto-converts ISO-formatted dates) and ISO-shaped strings. Returns
    "" for unset / unparseable values — the recall layer treats "" as
    "no expiry" (decay = 1.0).
    """
    if value is None or value == "":
        return ""
    # datetime.date or datetime.datetime (yaml auto-converts ISO dates)
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    s = str(value).strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:10]
    return ""


def parse_provenance(value: Any) -> str:
    """Coerce a frontmatter ``provenance`` value to a string. Empty when unset."""
    if value is None:
        return ""
    return str(value).strip()


def note_title(metadata: dict[str, Any], stem: str) -> str:
    """Title for the FTS5 row: frontmatter ``title:`` if present, else stem."""
    t = metadata.get("title")
    if t:
        return str(t).strip()
    return stem


# Simple ``key: value`` scalar line inside a frontmatter block.
_FM_SCALAR_RE = re.compile(r"^([A-Za-z0-9_-]+):\s*(.*?)\s*$")


def _salvage_frontmatter(text: str) -> dict[str, Any]:
    """Best-effort scalar salvage from an UNPARSEABLE frontmatter block.

    Frontmatter that fails YAML (the classic: an unquoted ``tldr:`` scalar
    containing ``: `` mid-line) would otherwise drop the whole note from
    the index (#recall-embed-context-overflow follow-on — same vanish
    class as the embed failures). Most broken blocks are broken on ONE
    line; the rest are plain ``key: value`` scalars worth keeping —
    ``sensitivity`` above all, since the retrieval gate filters on it.
    Lines that aren't simple string scalars (lists, nesting, block
    scalars) are skipped; surrounding quotes are stripped.

    #no-mnpi-to-cloud fail-closed (was cited as §5.4): if no
    ``sensitivity:`` line survives the salvage,
    the note is marked MNPI — the broken block MIGHT have declared a tier
    we failed to read, so the note must never leak past a lower ceiling.
    (A note with NO frontmatter block at all never reaches this function;
    it parses cleanly to empty metadata and keeps the usual defaults.)
    """
    meta: dict[str, Any] = {}
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            if line.strip() in ("---", "..."):
                break
            m = _FM_SCALAR_RE.match(line)
            if not m:
                continue
            key, value = m.group(1), m.group(2)
            # Keep plain string scalars only — flow/block collections and
            # anchors would need real YAML and risk type surprises downstream.
            if not value or value[0] in "[{|>&*":
                continue
            if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
                value = value[1:-1]
            meta[key] = value
    meta.setdefault("sensitivity", "MNPI")
    return meta


# Chunking config — sliding 500-word window with 50-word overlap. The
# 500-word size keeps a typical chunk well under the embed context wall
# (see EMBED_MAX_CHARS below) while keeping the chunk count tractable
# (5000-word doc → ~11 chunks).
CHUNK_WORDS = 500
CHUNK_OVERLAP = 50

# Embed-input budget (#recall-embed-context-overflow, 2026-06-11).
#
# nomic-embed-text's REAL context is 2048 wordpiece tokens — the GGUF's
# ``nomic-bert.context_length``, a hard wall the runner clamps to. The
# Ollama modelfile advertises ``num_ctx 8192`` but that does NOT raise the
# wall: past 2048 tokens /api/embeddings 500s ("the input length exceeds
# the context length") and even /api/embed with ``truncate: true`` 400s.
# So the input must be capped client-side; no server knob fixes it.
#
# Probe-measured (2026-06-11, the 14 failing vault notes): the densest
# markdown (tables / wikilink runs) tokenises at 3.15–3.88 chars/token;
# prose is ~4+. EMBED_MAX_CHARS = 6000 ≈ 1900 tokens at the densest
# measured ratio — under the wall with headroom, while keeping ~1500
# tokens of signal for ordinary prose. A pathological note denser than
# anything measured overflows once and is retried at EMBED_RETRY_CHARS:
# wordpiece emits at most one token per character, so 1800 chars can
# NEVER exceed the 2048-token wall — the retry is mathematically safe.
EMBED_MAX_CHARS = 6000
EMBED_RETRY_CHARS = 1800


def embed_capped(client: OllamaClient, model: str, text: str) -> list[float]:
    """Embed ``text`` under the embed model's hard context wall.

    First attempt at ``EMBED_MAX_CHARS``; if the server still reports a
    context overflow (content denser than anything measured), retry once
    at ``EMBED_RETRY_CHARS``, which cannot overflow (≥1 char per wordpiece
    token). Non-overflow failures (Ollama down, transport faults) are
    re-raised unchanged — the caller decides whether to degrade.
    """
    try:
        return client.embed(model=model, text=text[:EMBED_MAX_CHARS])
    except OllamaError as e:
        # Overflow detection is wording-tolerant (codex r1 SEV-3): today's
        # Ollama says "the input length exceeds the context length", but
        # match case-insensitively on context + length/window so a future
        # rewording still gets the recoverable retry. A miss is safe — the
        # caller degrades to lexical and health surfaces it.
        msg = str(e).lower()
        if "context" not in msg or ("length" not in msg and "window" not in msg):
            raise
        logger.warning(
            "embed input overflowed context at %d chars — retrying at %d: %s",
            min(len(text), EMBED_MAX_CHARS), EMBED_RETRY_CHARS, e,
        )
        return client.embed(model=model, text=text[:EMBED_RETRY_CHARS])


def chunk_body(body: str, *, size: int = CHUNK_WORDS, overlap: int = CHUNK_OVERLAP) -> list[dict[str, Any]]:
    """Split a body into overlapping word-windowed chunks.

    Returns a list of dicts: {idx, text, char_start, char_end}. char_start/end
    are byte offsets into the original body string, useful for re-locating
    the chunk if we want to highlight or excerpt it later.
    """
    body = body or ""
    if not body.strip():
        return []
    # Split on whitespace, keep track of positions in the original string.
    tokens: list[tuple[str, int, int]] = []  # (word, start, end)
    pos = 0
    while pos < len(body):
        # Skip whitespace
        while pos < len(body) and body[pos].isspace():
            pos += 1
        if pos >= len(body):
            break
        start = pos
        while pos < len(body) and not body[pos].isspace():
            pos += 1
        tokens.append((body[start:pos], start, pos))
    if not tokens:
        return []
    chunks: list[dict[str, Any]] = []
    step = max(1, size - overlap)
    i = 0
    idx = 0
    while i < len(tokens):
        window = tokens[i : i + size]
        if not window:
            break
        char_start = window[0][1]
        char_end = window[-1][2]
        text = body[char_start:char_end]
        chunks.append({
            "idx": idx,
            "text": text,
            "char_start": char_start,
            "char_end": char_end,
        })
        idx += 1
        if i + size >= len(tokens):
            break
        i += step
    return chunks


def open_db(db_path: Path) -> sqlite3.Connection:
    """Open (and create if missing) the index DB."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    return conn


# ============================================================ embedding pack


def pack_embedding(vec: list[float]) -> bytes:
    """Pack a list of floats as little-endian float32 blob."""
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack_embedding(blob: bytes) -> list[float]:
    """Inverse of pack_embedding."""
    n = len(blob) // 4
    return list(struct.unpack(f"<{n}f", blob))


# ============================================================ core


def index_vault(
    vault_root: Path,
    *,
    client: OllamaClient,
    embed_model: str = DEFAULT_EMBED_MODEL,
    rebuild: bool = False,
    db_path: Path | None = None,
) -> dict[str, int]:
    """Build or refresh the embedding index for the vault.

    Args:
        vault_root: vault root dir
        client: OllamaClient for embeddings
        embed_model: Ollama model name
        rebuild: if True, re-embed everything; else only changed files
        db_path: override index location (default: <vault>/.recall-index/index.db)

    Returns dict with counts: {scanned, added, updated, unchanged, removed, errors}.
    """
    if db_path is None:
        db_path = vault_root / DEFAULT_INDEX_DIR / DEFAULT_INDEX_DB

    counts = {
        "scanned": 0, "added": 0, "updated": 0, "unchanged": 0,
        "removed": 0, "errors": 0, "chunks": 0, "fts_backfill": 0,
        "embed_degraded": 0, "chunk_errors": 0, "fm_salvaged": 0,
    }
    # Paths behind the failure counters — persisted with the run stats so
    # ``recall health`` can name what needs attention (a hard-errored note
    # leaves NO db row, so the stats row is its only trace).
    error_paths: list[str] = []
    degraded_paths: list[str] = []
    salvaged_paths: list[str] = []
    conn = open_db(db_path)

    if rebuild:
        conn.execute("DELETE FROM notes")
        conn.execute("DELETE FROM chunks")
        conn.execute("DELETE FROM recall_fts")
        conn.commit()
        logger.info("rebuild: cleared index (notes + chunks + recall_fts)")

    # path -> (file_hash, has_embedding). A row with a NULL embedding is a
    # lexical-only degraded write (embed failed at index time) — it is
    # deliberately NOT eligible for the unchanged fast path, so every
    # subsequent run retries the embed until it heals.
    existing = {
        row[0]: (row[1], bool(row[2]))
        for row in conn.execute(
            "SELECT path, file_hash, embedding IS NOT NULL FROM notes"
        )
    }
    existing_fts: set[str] = {row[0] for row in conn.execute("SELECT path FROM recall_fts")}
    seen: set[str] = set()

    for note_path in _walk_vault_notes(vault_root):
        counts["scanned"] += 1
        rel_path = str(note_path.relative_to(vault_root).as_posix())
        seen.add(rel_path)
        try:
            file_hash = _hash_file(note_path)
            prev_hash, prev_embedded = existing.get(rel_path, (None, False))
            embedding_valid = prev_hash == file_hash and prev_embedded

            # Fast path: hash unchanged with a valid embedding AND FTS5 row
            # already present. Nothing to do.
            if embedding_valid and rel_path in existing_fts:
                counts["unchanged"] += 1
                continue

            # Read + parse (needed for FTS5 backfill OR for re-embed).
            # A malformed frontmatter block degrades to a line-level scalar
            # salvage (fail-closed on sensitivity) with the FULL text as
            # body — never drop the note over one broken YAML line.
            text = note_path.read_text(encoding="utf-8", errors="replace")
            try:
                post = frontmatter.loads(text)
                metadata = dict(post.metadata)
                body = post.content
            except Exception as e:  # noqa: BLE001 — yaml raises many types
                metadata = _salvage_frontmatter(text)
                body = text
                counts["fm_salvaged"] += 1
                salvaged_paths.append(rel_path)
                logger.warning(
                    "frontmatter unparseable for %s — salvaged %d scalar key(s); "
                    "fix the YAML at source: %s",
                    rel_path, len(metadata), e,
                )

            # Frontmatter triad — populated for FTS5 in both branches below.
            fts_title = note_title(metadata, note_path.stem)
            fts_importance = parse_importance(metadata.get("importance"))
            fts_expires = parse_expires_iso(metadata.get("expires"))
            fts_provenance = parse_provenance(metadata.get("provenance"))

            # Idempotent FTS5 write — runs in both branches (backfill + re-embed).
            conn.execute("DELETE FROM recall_fts WHERE path = ?", (rel_path,))
            conn.execute(
                "INSERT INTO recall_fts (path, title, body, importance, "
                "expires_iso, provenance) VALUES (?, ?, ?, ?, ?, ?)",
                (rel_path, fts_title, body or "", fts_importance,
                 fts_expires, fts_provenance),
            )

            # Hash unchanged with a valid embedding but FTS5 row was missing
            # → backfill-only path. Skip the (expensive) embedding work;
            # notes + chunks rows are already valid. (A degraded row — NULL
            # embedding — falls through to the re-embed below instead.)
            if embedding_valid:
                counts["fts_backfill"] += 1
                counts["unchanged"] += 1
                continue

            # Build the text we embed: title + tldr + first chunk of body.
            # Simple and effective for retrieval purposes. embed_capped owns
            # the context-wall cap (EMBED_MAX_CHARS) — body is pre-trimmed
            # here only to keep the assembled string small.
            title = note_path.stem
            tldr = str(metadata.get("tldr", "")).strip()
            embed_text = "\n\n".join(filter(None, [
                f"# {title}",
                tldr,
                body[:EMBED_MAX_CHARS],
            ]))
            if not embed_text.strip():
                logger.warning("skipping empty note: %s", rel_path)
                counts["errors"] += 1
                error_paths.append(rel_path)
                continue

            # Embed failure degrades to a LEXICAL-ONLY row rather than
            # skipping the note (#recall-embed-context-overflow): the FTS5
            # row above plus the notes row below (NULL embedding) keep the
            # note visible to /recall's FTS lane — retrieval filters build
            # ``notes_by_path`` from the notes table, so without the row the
            # note would vanish from BOTH lanes. The NULL embedding marks it
            # for re-embed on the next run (see ``existing`` above).
            vec: list[float] | None = None
            try:
                vec = embed_capped(client, embed_model, embed_text)
            except OllamaError as e:
                logger.warning(
                    "note embed failed for %s — writing lexical-only row: %s",
                    rel_path, e,
                )
                counts["embed_degraded"] += 1
                degraded_paths.append(rel_path)

            conn.execute(
                "INSERT OR REPLACE INTO notes "
                "(path, file_hash, frontmatter_json, tldr, body_excerpt, "
                " modified_at, indexed_at, embedding_model, embedding) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rel_path,
                    file_hash,
                    json.dumps(metadata, default=str),
                    tldr,
                    body[:8000],
                    datetime.fromtimestamp(note_path.stat().st_mtime).isoformat(),
                    datetime.now().isoformat(),
                    embed_model if vec is not None else None,
                    pack_embedding(vec) if vec is not None else None,
                ),
            )

            # ── Chunked embedding ────────────────────────────────────────
            # Replace this file's chunks (stale either way). When the
            # whole-note embed degraded, skip the chunk pass entirely — the
            # chunk inputs are capped below the context wall, so a
            # note-level failure means a server-level fault that would just
            # fail once per chunk (and the NULL embedding already queues
            # the note for a full re-embed next run).
            conn.execute("DELETE FROM chunks WHERE path = ?", (rel_path,))
            chunks = chunk_body(body) if vec is not None else []
            for ch in chunks:
                # Prefix each chunk with the title so cross-file semantic
                # signal isn't lost (chunks deep in long docs share the
                # title context). embed_capped trims to the context wall —
                # a 500-word window of long-token content (paths, wikilink
                # runs) can otherwise exceed it.
                chunk_embed_text = f"# {title}\n\n{ch['text']}"
                try:
                    cvec = embed_capped(client, embed_model, chunk_embed_text)
                except Exception as e:  # noqa: BLE001
                    logger.warning("chunk embed failed for %s#%d: %s", rel_path, ch["idx"], e)
                    counts["chunk_errors"] += 1
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO chunks "
                    "(path, chunk_idx, chunk_text, char_start, char_end, "
                    " file_hash, embedding_model, embedding) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rel_path,
                        ch["idx"],
                        ch["text"],
                        ch["char_start"],
                        ch["char_end"],
                        file_hash,
                        embed_model,
                        pack_embedding(cvec),
                    ),
                )
                counts["chunks"] += 1

            if rel_path in existing:
                counts["updated"] += 1
            else:
                counts["added"] += 1

        except Exception as e:  # noqa: BLE001
            logger.exception("error indexing %s: %s", rel_path, e)
            counts["errors"] += 1
            error_paths.append(rel_path)

    # Remove rows for files that no longer exist. Cover the FTS5 sidecar
    # too so deleted files don't haunt hybrid recall.
    to_remove = (set(existing.keys()) | existing_fts) - seen
    if to_remove:
        conn.executemany(
            "DELETE FROM notes WHERE path = ?",
            [(p,) for p in to_remove],
        )
        conn.executemany(
            "DELETE FROM chunks WHERE path = ?",
            [(p,) for p in to_remove],
        )
        conn.executemany(
            "DELETE FROM recall_fts WHERE path = ?",
            [(p,) for p in to_remove],
        )
        counts["removed"] = len(to_remove)
        logger.info("removed %d stale rows (notes + chunks + recall_fts)", len(to_remove))

    # Persist run stats so ``recall health`` (and the vault_health sweep)
    # can surface errors after the fact. A hard-errored note leaves no
    # notes/chunks/fts row, so this row is the only durable trace of it.
    conn.execute(
        "INSERT INTO index_runs (run_at, counts_json) VALUES (?, ?)",
        (
            datetime.now().isoformat(),
            json.dumps({
                **counts,
                "error_paths": error_paths,
                "degraded_paths": degraded_paths,
                "salvaged_paths": salvaged_paths,
            }),
        ),
    )
    # rowid is the authoritative append order (codex r1 SEV-3 — a local
    # clock step would scramble run_at ordering); run_at is display-only.
    conn.execute(
        "DELETE FROM index_runs WHERE rowid NOT IN "
        "(SELECT rowid FROM index_runs ORDER BY rowid DESC LIMIT ?)",
        (_INDEX_RUNS_KEEP,),
    )

    conn.commit()
    conn.close()
    return counts


# ============================================================ helpers


def index_health(db_path: Path) -> dict[str, Any]:
    """Read-only index health snapshot for ``recall health`` + vault_health.

    Returns::

        {
          "exists": bool,            # db file present
          "notes": int,              # rows in notes
          "degraded": int,           # notes with NULL embedding (lexical-only)
          "degraded_paths": [str],   # their paths
          "last_run": dict | None,   # newest index_runs counts (incl.
                                     # run_at / error_paths / degraded_paths)
        }

    Never raises on a pre-#recall-embed-context-overflow db — a missing
    ``index_runs`` table reads as ``last_run: None``.
    """
    if not db_path.exists():
        return {"exists": False, "notes": 0, "degraded": 0,
                "degraded_paths": [], "last_run": None}
    conn = sqlite3.connect(str(db_path))
    try:
        notes = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        degraded_paths = [
            row[0] for row in conn.execute(
                "SELECT path FROM notes WHERE embedding IS NULL ORDER BY path"
            )
        ]
        last_run: dict[str, Any] | None = None
        try:
            # Newest by rowid — the append order; run_at is display-only.
            row = conn.execute(
                "SELECT run_at, counts_json FROM index_runs "
                "ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        except sqlite3.OperationalError:
            row = None  # older index db without the index_runs table
        if row is not None:
            try:
                last_run = {"run_at": row[0], **json.loads(row[1])}
            except json.JSONDecodeError:
                last_run = {"run_at": row[0]}
    finally:
        conn.close()
    return {
        "exists": True,
        "notes": notes,
        "degraded": len(degraded_paths),
        "degraded_paths": degraded_paths,
        "last_run": last_run,
    }


def _walk_vault_notes(vault_root: Path) -> Iterator[Path]:
    """Yield all .md files under vault_root, skipping SKIP_DIRS / SKIP_FILES."""
    skip_paths = {(vault_root / d).resolve() for d in SKIP_DIRS}
    skip_files = {(vault_root / f).resolve() for f in SKIP_FILES}
    for path in vault_root.rglob("*.md"):
        resolved = path.resolve()
        # Skip if any ancestor is in SKIP_DIRS
        if any(skip in resolved.parents for skip in skip_paths):
            continue
        # Skip the path itself if it matches a skip dir or skip file
        if resolved in skip_paths or resolved in skip_files:
            continue
        # Skip rule files at ANY depth (#claudemd-restructure, 2026-06-11):
        # the constitution set (root + Projects/ + Templates/ + the _claude
        # stub) is procedural memory, not content — indexing it pollutes
        # content recall with rule text. SKIP_FILES above is top-level-only.
        if path.name == "CLAUDE.md":
            continue
        yield path


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
