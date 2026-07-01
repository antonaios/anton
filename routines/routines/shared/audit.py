"""Append-only audit log for routine runs + structured activity log (#60).

There are two related write surfaces here:

  1. **Legacy ``write(routine, run_id, status, *, audit_dir, ...)``** — the
     per-routine JSONL writer that 15+ existing call sites depend on
     (``hinotes``, ``sectornews``, ``memory-promote``, ``dealtracker``,
     ``projects.actions.toggle``, ``projects.overview``, …). Persists one
     line to ``<audit_dir>/<routine>.jsonl``. Signature + behaviour are
     UNCHANGED — that's the load-bearing back-compat contract.

  2. **New ``write_structured(*, actor, entity_type, entity_id, action,
     run_id=None, details=None, ts=None)``** — the canonical structured
     API going forward. ActorRef + EntityType closed enum + required
     kwargs (TypeError on omission). Persists to:
       * ``routines/runs/activity.jsonl`` — single unified JSONL stream
       * ``routines/state/audit_index.db`` — queryable SQLite index
     Emits ``ActivityLogged`` on the bridge event bus per write so the
     dashboard can subscribe via SSE in future #69 work.

Every legacy ``write()`` call additionally routes a derived structured
record through the same pipeline (sanitize → SQLite → event), so the
new infrastructure is exercised for every write from day 1. This loses
some entity-type fidelity on the legacy sites (everything defaults to
``entity_type="session"``); ``#60-migrate-sites`` is the deferred follow-on
that migrates individual sites to real actor + entity declarations.

Pipeline order (canonical):
  build_record → sanitize_record → redact_record → persist (JSONL +
  SQLite) → emit ActivityLogged

  * ``sanitize_record`` is **unconditional** — strips API-key patterns
    + truncates base64-ish blobs >1024 chars. Not operator-configurable;
    a leaked ``sk-ant-…`` in audit JSONL is a real risk.
  * ``redact_record`` is **opt-in** — reads optional
    ``<vault>/_claude/redaction.yaml`` for codename mappings.
    Absent file → pass-through (don't refuse the write).

#68 — every audit/telemetry write is wrapped by ``@safe_audit`` so a
failed write never aborts the user-facing skill that triggered it.
Failures land in ``routines/metrics/audit_failures.jsonl`` so they stay
visible somewhere. Observability is not load-bearing logic; the skill
matters, the audit is telemetry. Wrong-priority crash.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


# ────────────────────────────────────────────────────────────────────────────
# Closed enums — ActorType + EntityType (#60)
# ────────────────────────────────────────────────────────────────────────────


ActorType = Literal["user", "system", "agent", "plugin"]
ACTOR_TYPES: tuple[str, ...] = ("user", "system", "agent", "plugin")

EntityType = Literal[
    "session",
    "skill_run",
    "vault_note",
    "proposal",
    "workspace",
    "budget",
    "credential",
    "scheduler_job",
    "composite_run",
    "crew_run",
]
ENTITY_TYPES: tuple[str, ...] = (
    "session",
    "skill_run",
    "vault_note",
    "proposal",
    "workspace",
    "budget",
    "credential",
    "scheduler_job",
    "composite_run",
    "crew_run",
    "injection_scan",  # #sec-injection-guard 3a — ingestion-boundary content-trust detection
)


# ────────────────────────────────────────────────────────────────────────────
# Paths
# ────────────────────────────────────────────────────────────────────────────


def _failures_log_path() -> Path:
    """Resolve where ``audit_failures.jsonl`` lives.

    Lives under ``routines/metrics/`` (sibling to ``runs/``). Resolved
    through ``__file__`` so tests can monkeypatch by reassigning the
    module-level constant if needed."""
    from routines.shared import audit as self_mod
    return self_mod.AUDIT_FAILURES_LOG


AUDIT_FAILURES_LOG = (
    Path(__file__).resolve().parents[2] / "metrics" / "audit_failures.jsonl"
)

# Unified structured-activity JSONL stream (#60).
ACTIVITY_JSONL = (
    Path(__file__).resolve().parents[2] / "runs" / "activity.jsonl"
)

# Optional operator-authored redaction settings (codename mapping).
# Gitignored / encrypted at rest in the operator's vault. Absent file
# means redaction is a pass-through.
REDACTION_SETTINGS_PATH = Path("<vault>/_claude/redaction.yaml")


# ────────────────────────────────────────────────────────────────────────────
# @safe_audit decorator — #68
# ────────────────────────────────────────────────────────────────────────────


def safe_audit(fn: F) -> F:
    """Decorator: telemetry/audit writes never crash the caller.

    Swallows exceptions, logs a warning, appends a failure record to
    ``routines/metrics/audit_failures.jsonl`` so failed audits stay
    visible somewhere. Returns ``None`` on failure.

    Apply ONLY to observability writes (audit, telemetry). Do NOT apply
    to load-bearing logic (skill outputs, vault writes, state mutations) —
    those MUST raise on failure.
    """

    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001 — observability never breaks the caller
            logger.warning(
                "Audit write failed (non-fatal): %s — %r",
                fn.__name__, e,
                exc_info=True,
            )
            _append_failure_record(fn.__name__, e, args)
            return None

    return wrapped  # type: ignore[return-value]


def _append_failure_record(fn_name: str, exc: BaseException, args: tuple) -> None:
    """Best-effort failure record. Itself wrapped in try/except so a
    failing failures-log can never crash the caller."""
    try:
        log_path = _failures_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "fn": fn_name,
            "error_class": type(exc).__name__,
            "error_message": str(exc),
            "args_sample": str(args)[:200],
        }
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:  # noqa: BLE001 — last-resort silence
        pass


# ────────────────────────────────────────────────────────────────────────────
# IDs + hashing helpers (load-bearing, not decorated)
# ────────────────────────────────────────────────────────────────────────────


def new_run_id() -> str:
    """8-char uuid for this run. Embed in output frontmatter."""
    return uuid.uuid4().hex[:8]


def hash_dict(d: dict[str, Any]) -> str:
    """Stable sha256 of a dict (sorted keys, JSON-serialised)."""
    blob = json.dumps(d, sort_keys=True, default=str).encode("utf-8")
    return f"sha256:{hashlib.sha256(blob).hexdigest()}"


def hash_file(path: Path) -> str:
    """sha256 of file contents."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


# ────────────────────────────────────────────────────────────────────────────
# Sanitize — UNCONDITIONAL (#60)
# ────────────────────────────────────────────────────────────────────────────


# Regex patterns for common API-key shapes. Add new ones as they emerge.
# Matching is permissive (covers Anthropic, OpenAI, GitHub PAT, bearer
# tokens, common env-var assignments). All matches are replaced with
# ``[REDACTED]`` in-place.
_API_KEY_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),               # Anthropic
    re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"),              # OpenAI project keys
    re.compile(r"sk-[A-Za-z0-9_\-]{32,}"),                   # OpenAI/legacy/generic sk-
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),                     # GitHub PAT
    re.compile(r"github_pat_[A-Za-z0-9_]{40,}"),             # GitHub fine-grained PAT
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}", re.IGNORECASE),
    re.compile(r"(?i)(ANTHROPIC|OPENAI|GITHUB|HF|HUGGINGFACE)_API_KEY\s*=\s*\S+"),
    re.compile(r"(?i)(ANTHROPIC|OPENAI|GITHUB)_TOKEN\s*=\s*\S+"),
    # ── Prefix-less / well-known token shapes (#audit-sanitize-coverage) ──────
    # High-confidence, LOW-false-positive: each carries a distinctive literal
    # prefix, so a PREFIX-LESS secret (a raw token with no "Bearer"/"*_API_KEY="
    # wrapper) is caught WITHOUT resorting to broad high-entropy redaction (which
    # damages audit utility). The complementary defence is field-name-aware value
    # redaction below — together they close the #audit-sanitize-coverage gap.
    re.compile(r"(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"),  # JWT (3 base64url segments; 'eyJ' = base64 of '{"'; left-boundary avoids matching mid-blob)
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),            # AWS access key id (long-term / temporary)
    re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"),            # Slack bot/user/app/refresh/legacy token
    re.compile(r"ya29\.[A-Za-z0-9_\-]{20,}"),                # Google OAuth2 access token
    re.compile(r"\bAIza[0-9A-Za-z_\-]{35}(?![0-9A-Za-z_\-])"),  # Google API key (lookahead, not \b — keys may end in '-')
    re.compile(r"glpat-[A-Za-z0-9_\-]{20,}"),                # GitLab personal access token
]

# Curated dict-KEY names whose STRING value is a secret regardless of the
# value's shape (#audit-sanitize-coverage). Normalised (lowercased; ``_``, ``-``
# and spaces removed) so ``API-Key`` / ``api_key`` / ``apiKey`` all match.
# Deliberately EXCLUDES ambiguous bare names (``key`` / ``token`` / ``id``) —
# redacting those would damage audit utility with false positives. The list is
# intentionally CURATED, not exhaustive: it covers the high-confidence
# secret-bearing names; an unusual key (``connection_string``, ``password_hash``)
# is left to the prefix-pattern set / the source-level fixes (this is a
# best-effort backstop, not the load-bearing protection). Field-name redaction
# complements the prefix-pattern set: it catches an OPAQUE secret (no
# recognisable token shape) that nonetheless sits under an obviously-secret key.
_SENSITIVE_KEY_NAMES = frozenset({
    "apikey", "secret", "secretkey", "clientsecret", "password", "passwd",
    "passphrase", "accesstoken", "refreshtoken", "idtoken", "authtoken",
    "sessiontoken", "bearertoken", "authorization", "privatekey",
    "secrettoken", "tokensecret", "signingsecret", "webhooksecret",
    "credential", "credentials",
})


def _normalize_key(k: str) -> str:
    """Lowercase + strip ``_`` / ``-`` / space so ``access_token`` /
    ``Access-Token`` / ``accessToken`` normalise to the same comparison key.
    (Does NOT normalise dots/colons — ``api.key`` won't match; that shape is
    rare in our audit payloads and not worth the false-positive surface.)"""
    return k.lower().replace("_", "").replace("-", "").replace(" ", "")


def _is_sensitive_key(k: str) -> bool:
    """True if ``k`` is a curated secret-bearing field name (exact normalised
    match — NOT substring, to avoid e.g. ``token_count`` matching)."""
    return _normalize_key(k) in _SENSITIVE_KEY_NAMES

# Threshold for base64-like-blob truncation. Strings longer than this AND
# made of >90% [A-Za-z0-9+/=_-] characters get truncated to the first 64
# chars + "...[truncated <N> chars]". Below 1024 we leave alone — even
# legitimately long natural-language details are rarely above this.
_BASE64_TRUNCATE_THRESHOLD = 1024
_BASE64_CHARSET_RE = re.compile(r"^[A-Za-z0-9+/=_\-\n\r]+$")


def _sanitize_string(s: str) -> str:
    """Apply API-key regex substitution + base64-blob truncation."""
    if not isinstance(s, str):
        return s  # defensive — caller already gates this
    out = s
    for pat in _API_KEY_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    if (
        len(out) > _BASE64_TRUNCATE_THRESHOLD
        and _BASE64_CHARSET_RE.match(out) is not None
    ):
        kept = out[:64]
        truncated_len = len(out) - 64
        out = f"{kept}...[truncated {truncated_len} chars]"
    return out


def _sanitize_value(v: Any) -> Any:
    """Recursive sanitizer for dict/list/str values."""
    if isinstance(v, str):
        return _sanitize_string(v)
    if isinstance(v, dict):
        # Scrub string KEYS too — a secret used as a dict key (e.g.
        # ``{"sk-ant-…": "x"}``) would otherwise survive the value-only pass
        # (codex-5.5). Two keys collapsing to the same redaction is acceptable
        # (a secret-as-key is pathological).
        #
        # Field-name-aware value redaction (#audit-sanitize-coverage): a STRING
        # value under a curated sensitive key (``api_key`` / ``access_token`` /
        # ``password`` / …) is redacted WHOLE regardless of its shape — this
        # catches an opaque secret with no recognisable token prefix. A
        # NON-string value (nested dict/list) is recursed instead of
        # blanket-redacted, preserving audit structure while still scrubbing
        # nested strings by pattern.
        out: dict[Any, Any] = {}
        for k, val in v.items():
            k2 = _sanitize_string(k) if isinstance(k, str) else k
            if isinstance(k, str) and isinstance(val, str) and _is_sensitive_key(k):
                out[k2] = "[REDACTED]"
            else:
                out[k2] = _sanitize_value(val)
        return out
    if isinstance(v, list):
        return [_sanitize_value(item) for item in v]
    if isinstance(v, tuple):
        return tuple(_sanitize_value(item) for item in v)
    return v


def sanitize_record(record: dict[str, Any]) -> dict[str, Any]:
    """Strip API keys + truncate base64-ish blobs from a structured record.

    Always applied; not operator-configurable. Operates on the
    ``details`` field — top-level structural fields (``actor``,
    ``entity_type``, ``action``, …) are passed through unchanged.

    Returns a new dict — does NOT mutate ``record`` in place.
    """
    out = dict(record)
    details = record.get("details")
    if details is None:
        return out
    out["details"] = _sanitize_value(details)
    return out


# ────────────────────────────────────────────────────────────────────────────
# Redact — OPT-IN (#60)
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class RedactSettings:
    """Operator-configurable codename mapping.

    ``codenames`` is a dict of ``{real_name: codename}``. Any string value
    inside the record's ``details`` containing a key will be replaced
    with the codename. Substitution is plain-text (no regex), case-
    sensitive (operator authors both sides in the same casing).

    Future fields can land here (e.g. ``hide_emails``, ``hash_ips``) —
    keep the shape additive.
    """

    codenames: dict[str, str] = field(default_factory=dict)


# ── redaction.yaml mtime-cache (#eff-hotpath-batch) ──────────────────────────
# BEFORE: ``write_structured`` → ``redact_record()`` → ``_load_redaction_settings``
# did ``import yaml`` + ``yaml.safe_load(file)`` on EVERY structured audit write,
# re-reading + re-parsing redaction.yaml each time. AFTER: the parsed settings
# are cached per resolved path, keyed on the file's (mtime_ns, size). An operator
# edit changes the mtime → the next write reparses, so a codename change still
# takes effect; an absent-file result is cached too (keyed on a sentinel) so the
# common "no redaction configured" case does zero filesystem work after the
# first miss. Process-local; the lock keeps the cache dict consistent under the
# concurrent-writer load (threadpool + APScheduler).
_redaction_cache: dict[str, tuple[tuple[int, int] | None, RedactSettings]] = {}
_redaction_cache_lock = threading.Lock()


def _redaction_stat_key(target: Path) -> tuple[int, int] | None:
    """Return (mtime_ns, size) for ``target``, or ``None`` if it's absent.

    The ``None`` sentinel lets us cache the "file does not exist" outcome
    distinctly from any real stat, so the opt-in-and-unused path (no
    redaction.yaml) is also served from cache after the first check.

    Why ``(mtime_ns, size)`` is sufficient and content-hashing was
    deliberately NOT used: NTFS records mtime with 100ns resolution
    (``st_mtime_ns``), so any operator edit — which happens at a later
    wall-clock time than the previous write — always bumps ``mtime_ns``. A
    change that is BOTH same-tick (sub-100ns) AND same-size is infeasible for
    an operator-edited file, so this signature never misses a real edit.
    Content-hashing would force a full file READ on every call, which would
    regress the very #eff-hotpath-batch optimization this cache exists for
    (avoiding a read+parse of redaction.yaml on every structured audit
    write). The (mtime_ns, size) pair gets invalidation for the cost of a
    single ``stat``.
    """
    try:
        st = target.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _load_redaction_settings(path: Path | None = None) -> RedactSettings:
    """Read ``<vault>/_claude/redaction.yaml`` if present (mtime-cached).

    Absent file / unreadable file → return empty settings (feature is
    opt-in; never refuse to write). Parsed settings are cached and only
    re-parsed when the file's mtime/size changes (operator edit) — so a
    codename change still takes effect on the next write without re-reading
    + re-parsing YAML on every audit write.
    """
    target = path or REDACTION_SETTINGS_PATH
    key = str(target)
    stat_key = _redaction_stat_key(target)

    # Fast path: cached entry whose stat signature still matches.
    cached = _redaction_cache.get(key)
    if cached is not None and cached[0] == stat_key:
        return cached[1]

    settings = _parse_redaction_settings(target, stat_key)
    with _redaction_cache_lock:
        _redaction_cache[key] = (stat_key, settings)
    return settings


def _parse_redaction_settings(
    target: Path, stat_key: tuple[int, int] | None,
) -> RedactSettings:
    """Actually read + parse redaction.yaml. Returns empty settings on any
    failure (opt-in feature — never refuse the audit write)."""
    if stat_key is None:
        # File absent (or unstattable) → empty settings (cached upstream).
        return RedactSettings()
    try:
        import yaml  # local import — pyyaml is already a dep (frontmatter)
        with target.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        codenames = data.get("codenames") or {}
        if not isinstance(codenames, dict):
            logger.warning(
                "redaction.yaml: 'codenames' must be a dict, got %s — ignoring",
                type(codenames).__name__,
            )
            return RedactSettings()
        # Coerce both sides to str so YAML quirks don't break us.
        return RedactSettings(codenames={str(k): str(v) for k, v in codenames.items()})
    except Exception as e:  # noqa: BLE001 — config read must never crash audit
        logger.warning("redaction settings load failed (non-fatal): %r", e)
        return RedactSettings()


def _apply_codenames(v: Any, codenames: dict[str, str]) -> Any:
    """Recursive codename substitution across dict/list/str values."""
    if isinstance(v, str):
        out = v
        for real, code in codenames.items():
            if real and real in out:
                out = out.replace(real, code)
        return out
    if isinstance(v, dict):
        return {k: _apply_codenames(val, codenames) for k, val in v.items()}
    if isinstance(v, list):
        return [_apply_codenames(item, codenames) for item in v]
    if isinstance(v, tuple):
        return tuple(_apply_codenames(item, codenames) for item in v)
    return v


def redact_record(
    record: dict[str, Any],
    settings: RedactSettings | None = None,
) -> dict[str, Any]:
    """Operator-configurable codename hiding. Applied AFTER sanitize.

    Settings live at ``<vault>/_claude/redaction.yaml`` (operator-
    authored, gitignored or encrypted at rest); absent file → pass-through.

    Returns a new dict — does NOT mutate ``record`` in place.
    """
    if settings is None:
        settings = _load_redaction_settings()
    if not settings.codenames:
        return record
    out = dict(record)
    details = record.get("details")
    if details is None:
        return out
    out["details"] = _apply_codenames(details, settings.codenames)
    return out


# ────────────────────────────────────────────────────────────────────────────
# Persist + emit (used by write_structured)
# ────────────────────────────────────────────────────────────────────────────


def _persist_activity_jsonl(record: dict[str, Any]) -> None:
    """Append the structured record to ``routines/runs/activity.jsonl``."""
    ACTIVITY_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with ACTIVITY_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _persist_sqlite(record: dict[str, Any]) -> None:
    """Insert one row into the queryable SQLite index. Best-effort —
    wrapped by the caller's try/except so a locked DB never crashes the
    user-facing skill."""
    from routines.shared import audit_db
    audit_db.insert_audit(record)


def _emit_activity_logged(record: dict[str, Any]) -> None:
    """Emit ``ActivityLogged`` on the bridge event bus per write.

    Best-effort — failures swallowed per #68 (audit is observability,
    never load-bearing). Wrapped in its own try/except so a missing /
    broken event-bus import doesn't crash the rest of the pipeline.
    """
    try:
        from routines.hooks import ActivityLogged, bridge_event_bus
        actor = record.get("actor") or {}
        bridge_event_bus.emit(ActivityLogged(
            run_id=str(record.get("run_id") or ""),
            actor_type=str(actor.get("type") or "system"),
            actor_id=str(actor.get("id") or "unknown"),
            action=str(record.get("action") or ""),
            entity_type=str(record.get("entity_type") or "session"),
            entity_id=str(record.get("entity_id") or "unknown"),
            details=record.get("details"),
            ts=str(record.get("ts") or ""),
        ))
    except Exception as e:  # noqa: BLE001 — observability never breaks the caller
        logger.warning(
            "activity.logged emit failed (non-fatal): %r — swallowing per "
            "#68 @safe_audit discipline", e,
        )


# ────────────────────────────────────────────────────────────────────────────
# Legacy per-routine JSONL record (shared by write() + write_structured)
# ────────────────────────────────────────────────────────────────────────────


def _build_legacy_record(
    routine: str,
    run_id: str,
    status: str,
    *,
    duration_ms: int | None = None,
    inputs: dict[str, Any] | None = None,
    outputs: dict[str, Any] | None = None,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
    episodic_source: Any = None,
    semantic_target: Any = None,
    procedural_target: Any = None,
    ts: str | None = None,
) -> dict[str, Any]:
    """Build the legacy per-routine JSONL record.

    This is the exact shape ``/api/audit-runs`` parses (``AuditRun``:
    ts, run_id, status, duration_ms, routine, inputs, outputs, error) plus
    the optional memory-lane fields. Shared by the legacy ``write()`` and
    by ``write_structured(routine=...)`` so the JSONL contract is identical
    regardless of which entry point produced the line (``#60-migrate-sites``
    collapses the old ``write(bridge=False)`` + ``write_structured`` dual
    write into a single ``write_structured`` call).
    """
    record: dict[str, Any] = {
        "ts": ts or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "routine": routine,
        "run_id": run_id,
        "status": status,
        "duration_ms": duration_ms,
    }
    if inputs:
        record["inputs"] = inputs
    if outputs:
        record["outputs"] = outputs
    if error:
        record["error"] = error
    if extra:
        record["extra"] = extra
    if episodic_source is not None:
        record["episodic_source"] = episodic_source
    if semantic_target is not None:
        record["semantic_target"] = semantic_target
    if procedural_target is not None:
        record["procedural_target"] = procedural_target
    return record


# Free-form legacy fields that can carry arbitrary content (and thus secrets /
# codenames). The STRUCTURAL fields (ts / routine / run_id / status /
# duration_ms) are deliberately NOT scrubbed — ``/api/audit-runs`` parses them
# by exact value (codex-5.5: confine redaction to the payload).
_LEGACY_PAYLOAD_FIELDS = (
    "inputs", "outputs", "error", "extra",
    "episodic_source", "semantic_target", "procedural_target",
)


def _sanitize_legacy_payload(record: dict[str, Any]) -> dict[str, Any]:
    """Scrub the FREE-FORM fields of a legacy per-routine record.

    The legacy JSONL carries ``inputs`` / ``error`` / ``outputs`` at the TOP
    LEVEL (not under ``details``), so ``sanitize_record`` — which only scrubs
    ``details`` — does NOT cover it. Without this, a secret accidentally placed
    in a routine's audit inputs/error lands in CLEARTEXT in
    ``runs/<routine>.jsonl`` (the surface ``/api/audit-runs`` reads), even though
    it IS stripped from the structured activity.jsonl / SQLite copy (codex-5.5,
    High). Apply the SAME recursive API-key strip + opt-in codename redaction the
    structured stream gets, but ONLY to the free-form fields so the structural
    contract is preserved. Returns a new dict — does NOT mutate ``record``."""
    out = dict(record)
    try:
        settings = _load_redaction_settings()
    except Exception:  # noqa: BLE001 — config read must never crash audit
        settings = RedactSettings()
    for field_name in _LEGACY_PAYLOAD_FIELDS:
        if out.get(field_name) is not None:
            scrubbed = _sanitize_value(out[field_name])
            if settings.codenames:
                scrubbed = _apply_codenames(scrubbed, settings.codenames)
            out[field_name] = scrubbed
    return out


def _persist_legacy_jsonl(record: dict[str, Any], audit_dir: Path) -> None:
    """Append one legacy record to ``<audit_dir>/<routine>.jsonl`` — AFTER
    scrubbing its free-form payload. This is the audit sink the otherwise-
    "unconditional" ``sanitize_record`` did NOT actually cover (codex-5.5)."""
    routine_name = str(record.get("routine") or "unknown")  # pre-scrub, for path
    record = _sanitize_legacy_payload(record)
    audit_dir.mkdir(parents=True, exist_ok=True)
    log_path = audit_dir / f"{routine_name}.jsonl"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, default=str) + "\n")


# ────────────────────────────────────────────────────────────────────────────
# write_structured — the canonical structured API (#60)
# ────────────────────────────────────────────────────────────────────────────


def _validate_actor(actor: Any) -> dict[str, str]:
    """Coerce + validate the ``actor`` arg to ``{type, id}``.

    Raises ``TypeError`` on missing/invalid shape — the caller has bugs
    and shouldn't be allowed to silently degrade the audit trail.
    """
    if not isinstance(actor, dict):
        raise TypeError(
            f"actor must be a dict with keys 'type' + 'id', got "
            f"{type(actor).__name__}"
        )
    atype = actor.get("type")
    aid = actor.get("id")
    if atype not in ACTOR_TYPES:
        raise TypeError(
            f"actor['type'] must be one of {ACTOR_TYPES}, got {atype!r}"
        )
    if not isinstance(aid, str) or not aid:
        raise TypeError(
            f"actor['id'] must be a non-empty str, got {aid!r}"
        )
    return {"type": atype, "id": aid}


def _validate_entity_type(entity_type: Any) -> str:
    """Coerce + validate entity_type against the closed enum."""
    if entity_type not in ENTITY_TYPES:
        raise TypeError(
            f"entity_type must be one of {ENTITY_TYPES}, got {entity_type!r}"
        )
    return str(entity_type)


def write_structured(
    *,
    actor: dict[str, str],
    entity_type: str,
    entity_id: str,
    action: str,
    run_id: str | None = None,
    details: dict[str, Any] | None = None,
    ts: str | None = None,
    # ── Optional legacy per-routine JSONL co-write (#60-migrate-sites) ──
    # Pass routine= (+ audit_dir=) ONLY at the allowlisted /api/audit-runs
    # sites (hinotes/sectornews/memory-promote/dealtracker/recall) so the
    # legacy <routine>.jsonl line keeps being produced. The remaining
    # legacy-shape fields below populate that line to the exact AuditRun
    # shape. Sites whose per-routine JSONL nothing reads omit routine=
    # entirely (their JSONL was dead output).
    routine: str | None = None,
    audit_dir: Path | None = None,
    status: str | None = None,
    duration_ms: int | None = None,
    inputs: dict[str, Any] | None = None,
    outputs: dict[str, Any] | None = None,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
    episodic_source: Any = None,
    semantic_target: Any = None,
    procedural_target: Any = None,
) -> None:
    """Structured activity-log write — the canonical API going forward.

    All kwargs are required (TypeError if absent — required-kwarg
    semantics via keyword-only ``*`` separator at the head). Pipeline:

      1. Build the record (timestamp + structured fields)
      2. ``sanitize_record`` (unconditional API-key strip + base64
         truncation)
      3. ``redact_record`` (opt-in operator codenames; pass-through if
         settings missing)
      4. Persist to ``routines/runs/activity.jsonl`` (primary; tail-f
         friendly) AND ``routines/state/audit_index.db`` (queryable
         index)
      5. Emit ``ActivityLogged`` on the bridge event bus (best-effort)

    Each persistence step is wrapped in its own try/except so partial
    failures don't break the chain. The outer ``@safe_audit`` decorator
    catches anything left, so a user-facing skill never crashes on an
    audit failure (#68 discipline).

    Args:
        actor: ``{"type": "user"|"system"|"agent"|"plugin", "id": str}``
        entity_type: one of ``ENTITY_TYPES``
        entity_id: arbitrary string ID — vault path, proposal id, etc.
        action: short verb-phrase action name (e.g. ``"reject"``,
            ``"vault_note.write"``)
        run_id: optional correlation id (link to a SkillInvocation /
            chat run / scheduler fire)
        details: arbitrary structured payload — will be sanitized +
            redacted before persistence
        ts: optional ISO-8601 UTC timestamp override (defaults to now)

    Validation errors (missing required kwarg, bad ``actor['type']``,
    bad ``entity_type``, empty ``entity_id`` / ``action``) raise
    ``TypeError`` — those are caller bugs and should surface loudly, not
    silently degrade the audit trail. Runtime persistence failures (disk
    full, SQLite locked, broken event bus) are caught per-step + logged
    as warnings + swallowed — observability never breaks the calling
    skill (#68 discipline). The pipeline is NOT wrapped in ``@safe_audit``
    so the TypeError path stays clean.
    """
    actor = _validate_actor(actor)
    entity_type = _validate_entity_type(entity_type)
    if not isinstance(entity_id, str) or not entity_id:
        raise TypeError(f"entity_id must be a non-empty str, got {entity_id!r}")
    if not isinstance(action, str) or not action:
        raise TypeError(f"action must be a non-empty str, got {action!r}")
    if routine is not None and audit_dir is None:
        raise TypeError(
            "write_structured(routine=...) requires audit_dir= so the legacy "
            "<routine>.jsonl line can be written"
        )

    # When co-writing a legacy row, mirror its payload into the structured
    # ``details`` if the caller didn't pass an explicit one — so the activity
    # feed / SQLite index carries the same context as the JSONL line without
    # the call site having to spell it twice (#60-migrate-sites).
    if details is None and routine is not None:
        mirrored: dict[str, Any] = {}
        if status:
            mirrored["status"] = status
        if inputs:
            mirrored["inputs"] = inputs
        if outputs:
            mirrored["outputs"] = outputs
        if error:
            mirrored["error"] = error
        details = mirrored or None

    record: dict[str, Any] = {
        "ts": ts or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "actor": actor,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "run_id": run_id,
        "details": details if details is not None else None,
    }

    # Sanitize + redact (always applied; redact may be no-op when no
    # settings file). Sanitize is unconditional API-key + base64 hygiene.
    try:
        record = sanitize_record(record)
        record = redact_record(record)
    except Exception as e:  # noqa: BLE001 — observability never breaks the caller
        # F-20 (CX B-07): a redaction layer that PERSISTS THE RAW record when
        # its own scrubber raised is the wrong failure mode — the un-scrubbed
        # (possibly secret-bearing) free-form fields would land in the audit
        # log. Replace the free-form fields (details / entity_id / actor) with a
        # redaction-failed marker; keep ONLY the controlled-vocab structural
        # fields (ts / action / entity_type / run_id) so the row's EXISTENCE +
        # correlation survive without leaking the content the scrubber couldn't
        # process.
        logger.warning(
            "sanitize/redact failed — writing a redaction-failed marker "
            "(NOT the raw record): %r", e,
        )

        def _safe_field(v: Any) -> Any:
            # Even the "structural" fields are NOT guaranteed controlled-vocab
            # (a caller could pass a secret-bearing action/entity_type), so
            # best-effort scrub them on this failure path too; if the scrubber
            # itself can't run, drop to a constant (codex-5.5 F-20 r1).
            if not isinstance(v, str):
                return v if v is None or isinstance(v, (int, float, bool)) else "[unavailable]"
            try:
                return _sanitize_string(v)
            except Exception:  # noqa: BLE001 — scrubber already failed once
                return "[unavailable]"

        record = {
            "ts": record.get("ts"),
            "actor": {"type": "system", "id": "audit.redaction_failed"},
            "action": _safe_field(record.get("action")),
            "entity_type": _safe_field(record.get("entity_type")),
            "entity_id": "[redaction-failed]",
            "run_id": _safe_field(record.get("run_id")),
            "details": {
                "_redaction_failed": True,
                "error_class": type(e).__name__,
            },
        }

    # Persist + emit. Each step independently wrapped so partial failures
    # don't break the rest. The legacy ``write()`` wrapper still has
    # @safe_audit on top for the JSONL-side belt-and-braces failure log.
    try:
        _persist_activity_jsonl(record)
    except Exception as e:  # noqa: BLE001
        logger.warning("activity.jsonl persist failed (non-fatal): %r", e)
    try:
        _persist_sqlite(record)
    except Exception as e:  # noqa: BLE001
        logger.warning("audit_index.db persist failed (non-fatal): %r", e)
    _emit_activity_logged(record)  # already wrapped in try/except internally

    # #60-migrate-sites — optional legacy per-routine JSONL co-write. When a
    # caller passes routine= this ALSO appends the legacy-shaped line to
    # <audit_dir>/<routine>.jsonl, preserving the /api/audit-runs contract
    # for the allowlisted routines. This is what lets a migrated site emit a
    # single write_structured() call instead of the old write(bridge=False)
    # + write_structured() dual write. Best-effort (observability never
    # crashes the caller); the ts is shared with the structured record above
    # so the two surfaces correlate.
    if routine is not None:
        try:
            legacy_record = _build_legacy_record(
                routine,
                run_id or "",
                status or "",
                duration_ms=duration_ms,
                inputs=inputs,
                outputs=outputs,
                error=error,
                extra=extra,
                episodic_source=episodic_source,
                semantic_target=semantic_target,
                procedural_target=procedural_target,
                ts=record["ts"],
            )
            _persist_legacy_jsonl(legacy_record, audit_dir)  # type: ignore[arg-type]
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "legacy <routine>.jsonl co-write failed (non-fatal): %r", e,
            )


# F-29 (HR audit-GAP1 + CX B-09, confirmed×2): the REQUEST-PATH variant.
# ``write_structured`` validates strictly and raises TypeError on a malformed
# actor/entity_id/action — correct for direct callers and tests, but a route
# handler whose FS action already SUCCEEDED must not 500 on its audit row
# (or on a full disk). Route-level callers use this alias; failures downgrade
# to a logged warning + failure record via ``safe_audit``.
write_structured_safe = safe_audit(write_structured)


# ────────────────────────────────────────────────────────────────────────────
# Legacy → structured bridge (used by write() so the new pipeline runs
# on every legacy write too)
# ────────────────────────────────────────────────────────────────────────────


def _legacy_action(routine: str, status: str) -> str:
    """Compose a structured ``action`` from the legacy ``routine`` +
    ``status`` fields. Format: ``"<routine>.<status>"`` — e.g.
    ``"hinotes.ok"``, ``"projects.actions.toggle.error"``."""
    return f"{routine}.{status}" if status else routine


def _legacy_entity_id(
    run_id: str,
    inputs: dict[str, Any] | None,
) -> str:
    """Best-effort entity_id heuristic for legacy writes.

    Picks the most-specific identifier available from ``inputs``,
    falling back to ``run_id`` so the field is never empty. The
    ``#60-migrate-sites`` follow-on will replace this heuristic with
    explicit ``entity_id`` declarations per call site.

    F-38 (CX B-06): the picked value is promoted to TOP-LEVEL ``entity_id``,
    which ``sanitize_record`` (details-only) never scrubs — so it passes
    through ``_sanitize_string`` here. The keys are curated identifiers, but
    ``path``/``source_file`` are caller-shaped strings that could carry a
    pasted secret."""
    if inputs:
        for key in ("session_id", "proposal_id", "project", "name", "id",
                    "workspace_name", "ticker", "path", "source_file"):
            v = inputs.get(key)
            if isinstance(v, str) and v:
                return _sanitize_string(v)
    return run_id or "unknown"


def _bridge_legacy_to_structured(
    routine: str,
    run_id: str,
    status: str,
    record: dict[str, Any],
    *,
    inputs: dict[str, Any] | None,
) -> None:
    """Derive a structured record from the legacy ``write()`` fields and
    route it through ``write_structured()``.

    Wrapped by the caller's outer ``@safe_audit`` so failures here can
    never crash the user-facing skill. Per the brief: legacy writes
    default to ``entity_type="session"`` — loses entity-fidelity vs
    explicit declarations, but acceptable for v1 (``#60-migrate-sites``
    fixes per-site).
    """
    details = {k: v for k, v in record.items() if k not in (
        "ts", "routine", "run_id", "status"
    )}
    write_structured(
        actor={"type": "system", "id": f"routine:{routine}"},
        entity_type="session",
        entity_id=_legacy_entity_id(run_id, inputs),
        action=_legacy_action(routine, status),
        run_id=run_id or None,
        details=details if details else None,
        ts=record.get("ts"),
    )


# ────────────────────────────────────────────────────────────────────────────
# Primary writer (legacy) — decorated so disk-full / locked-DB never
# crashes the caller
# ────────────────────────────────────────────────────────────────────────────


@safe_audit
def write(
    routine: str,
    run_id: str,
    status: str,
    *,
    audit_dir: Path,
    inputs: dict[str, Any] | None = None,
    outputs: dict[str, Any] | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
    episodic_source: Any = None,
    semantic_target: Any = None,
    procedural_target: Any = None,
    bridge: bool = True,
) -> None:
    """Append one audit record (legacy per-routine JSONL contract).

    Args:
        routine: e.g. "hinotes" / "sectornews" / "dealtracker"
        run_id: from new_run_id()
        status: "ok" | "skipped" | "error" | "partial"
        audit_dir: typically `<routines repo>/runs/`
        inputs: arbitrary dict — file paths, parameters, hashes
        outputs: dict — paths created, counts, summaries
        duration_ms: total time
        error: error message if status != "ok"
        extra: anything else
        episodic_source: optional — what episodic memory this run read
            (e.g. ``"~/.claude/projects/*/*.jsonl"`` for the learning loop's
            scan step, ``"Inbox/HiNotes/processed/<file>"`` for HiNotes).
        semantic_target: optional — what semantic memory was written or
            proposed (e.g. a ``Companies/<X>.md`` path, a list of paths,
            or ``"Registers/Decisions.md"``). Set by routines that promote
            episodic → semantic (memory-promote, deal-tracker).
        procedural_target: optional — what procedural memory was written or
            proposed (e.g. ``"Templates/company-profile.md"``). Set by
            routines that propose episodic → procedural edits (learning loop).
        bridge: when True (default) emit the generic ``system``/``session``
            structured row derived from the legacy fields. Pass
            ``bridge=False`` at sites that emit their own precise
            ``write_structured()`` record so the generic bridge is suppressed
            and we don't persist a double structured row (``#60-migrate-sites``).

    The three lane-transition fields are documented in
    ``Topics/Architecture/memory-model.md`` (Plan v3 §6.5 Phase B). Routines
    that don't move signals between memory lanes can leave them None.

    Behaviour preserved verbatim from the pre-#60 contract: writes one
    line to ``<audit_dir>/<routine>.jsonl``. Additionally (since #60)
    routes a derived structured record through the new pipeline
    (sanitize → SQLite → ``ActivityLogged`` event) so the new
    infrastructure is exercised on every legacy write too. The structured
    bridge is best-effort and never affects the legacy JSONL persist.

    Never raises — wrapped in ``@safe_audit``. A failed write logs a
    warning + appends to ``routines/metrics/audit_failures.jsonl`` so the
    caller (a user-facing skill) never crashes on observability failure.
    """
    record = _build_legacy_record(
        routine, run_id, status,
        duration_ms=duration_ms, inputs=inputs, outputs=outputs,
        error=error, extra=extra, episodic_source=episodic_source,
        semantic_target=semantic_target, procedural_target=procedural_target,
    )
    _persist_legacy_jsonl(record, audit_dir)
    logger.debug("audit written: routine=%s run_id=%s status=%s", routine, run_id, status)

    # #60 — route the legacy fields through the new structured pipeline so
    # SQLite + activity.jsonl + ActivityLogged event get exercised for every
    # legacy write. Best-effort; failures swallowed per @safe_audit. Sites
    # that emit their own precise write_structured() record pass bridge=False
    # to suppress this generic row and avoid double-counting.
    if bridge:
        try:
            _bridge_legacy_to_structured(
                routine, run_id, status, record, inputs=inputs,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "structured-pipeline bridge from legacy write failed "
                "(non-fatal — legacy JSONL row was persisted): %r", e,
            )


__all__ = [
    "AUDIT_FAILURES_LOG",
    "ACTIVITY_JSONL",
    "REDACTION_SETTINGS_PATH",
    "ACTOR_TYPES",
    "ENTITY_TYPES",
    "ActorType",
    "EntityType",
    "RedactSettings",
    "safe_audit",
    "new_run_id",
    "hash_dict",
    "hash_file",
    "sanitize_record",
    "redact_record",
    "write",
    "write_structured",
]
