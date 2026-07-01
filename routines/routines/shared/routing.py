"""Sensitivity router — picks the model lane for a given (task_type, sensitivity)
per CLAUDE.md §4 routing rules.

Bridge phase (Claude Max + ChatGPT Plus, no ZDR): confidential and MNPI
material does NOT touch any cloud lane.

Enterprise phase (Anthropic Enterprise + ZDR + ChatGPT Enterprise):
confidential can route to Claude Enterprise. MNPI still local-only.

The plan tier comes from the AGENTIC_PLAN_TIER env var, default "bridge".
Flip to "enterprise" the day Enterprise contracts land.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

PlanTier = Literal["bridge", "enterprise"]
Sensitivity = Literal["public", "internal", "confidential", "MNPI"]
TaskType = Literal[
    "transcript-extraction",   # HiNotes — defaults local always
    "triage",                   # quick classification — Haiku-grade work
    "classification",
    "format-word",              # generic Word formatting — MiniMax M3 OK if non-confidential
    "format-template-generic",
    "tone-polish-generic",
    "news-bulk",                # sector-news routine — Haiku-grade
    "synthesis",                # IC memo / one-pager / company-profile — Opus-grade
    "cross-check",              # Codex cross-check on consequential outputs
    "embed",                    # embeddings — always local
    "multimodal-extraction",   # parse images in PDFs / screenshots / charts — local gemma4:e4b
]

Lane = Literal[
    "ollama",                   # local, qwen3:14b
    "ollama-haiku",              # local, qwen3:8b (faster, smaller, "haiku-equivalent")
    "ollama-embed",              # local, nomic-embed-text
    "ollama-multimodal",         # local, gemma4:e4b — text + image + audio
    "claude-cli",                # cloud, Claude default (Opus-grade)
    "claude-cli-haiku",          # cloud, Claude Haiku
    "codex-cli",                 # cloud, OpenAI Codex
    "minimax",                   # cloud, MiniMax M3 — only for non-named generic format work
]


def plan_tier() -> PlanTier:
    """Read the LIVE plan tier from env. Default 'bridge' (safest).

    ``set_plan_tier`` writes ``os.environ`` in-process, so this reflects an
    operator UI flip immediately (no restart) for every routing decision; the
    persisted file (loaded at startup by ``load_persisted_plan_tier``) carries
    it across restarts.
    """
    val = os.environ.get("AGENTIC_PLAN_TIER", "bridge").strip().lower()
    if val not in ("bridge", "enterprise"):
        return "bridge"
    return val  # type: ignore[return-value]


# ── Runtime plan-tier control (#plan-tier-toggle) ────────────────────────────
# The plan tier is normally an env var read at launch. The operator-facing
# toggle (guarded ``POST /api/routing/plan-tier``) flips it at RUNTIME: it sets
# ``os.environ`` (effective immediately, since ``plan_tier`` reads it live) AND
# persists to ``routines/state/plan_tier.json`` so the choice survives a bridge
# restart. The persisted file is AUTHORITATIVE on boot — a UI flip outlives the
# launcher env. This is a confidentiality-boundary control: lifting to
# ``enterprise`` routes confidential material to cloud (the endpoint guards it
# with a nonce + an explicit acknowledgement). MNPI is unaffected here — it
# stays local until a separate P5 attestation, even at the enterprise tier.

_PLAN_TIER_STATE_DEFAULT = (
    Path(__file__).resolve().parents[2] / "state" / "plan_tier.json"
)
# Serialises concurrent flips so the persisted file and the live ``os.environ``
# can never disagree (the bridge runs sync handlers on an anyio threadpool +
# scheduler/event-bus worker threads).
_plan_tier_lock = threading.Lock()


def _plan_tier_state_path() -> Path:
    """Persisted-tier file path; ``AGENTIC_PLAN_TIER_STATE`` overrides (tests)."""
    override = os.environ.get("AGENTIC_PLAN_TIER_STATE")
    return Path(override) if override else _PLAN_TIER_STATE_DEFAULT


def set_plan_tier(tier: PlanTier, set_by: str) -> None:
    """Set the LIVE tier (``os.environ``, effective at once) + PERSIST it.

    Raises ``ValueError`` on an unknown tier. Under ``_plan_tier_lock`` so
    concurrent flips can't interleave; the persisted file is written FIRST
    (atomic tmp+replace) and ``os.environ`` only after — so the boot-authoritative
    file is never BEHIND the live env (a crash mid-flip re-seeds the intended
    tier, never silently reverts).
    """
    if tier not in ("bridge", "enterprise"):
        raise ValueError(f"invalid plan tier: {tier!r}")
    path = _plan_tier_state_path()
    payload = {
        "tier": tier,
        "set_by": set_by,
        "set_at": datetime.now(timezone.utc).isoformat(),
    }
    with _plan_tier_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
        tmp.replace(path)  # atomic on the same volume — persist BEFORE the env flip
        os.environ["AGENTIC_PLAN_TIER"] = tier


def load_persisted_plan_tier() -> None:
    """Seed ``os.environ`` from the persisted file at startup, if present.

    Called once in the bridge lifespan BEFORE any routing, so a UI-set tier is
    authoritative across restarts (it overrides whatever the launcher env set).

    Fail-safe semantics (a corrupt operator-state file must never silently keep
    cloud routing on):
      * file ABSENT → no-op, the launcher env / default applies (no operator
        override on record);
      * file PRESENT but unreadable / malformed / invalid-tier → force ``bridge``
        (never honour a possibly-stale ``enterprise`` env behind a bad file) +
        log it.
    """
    path = _plan_tier_state_path()
    with _plan_tier_lock:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return  # no operator override on record → env/default applies
        except OSError as e:
            os.environ["AGENTIC_PLAN_TIER"] = "bridge"
            logger.error("plan_tier: persisted state %s unreadable — forcing bridge: %s", path, e)
            return
        try:
            tier = str(json.loads(text)["tier"]).strip().lower()
        except (ValueError, KeyError, TypeError):
            tier = ""
        if tier in ("bridge", "enterprise"):
            os.environ["AGENTIC_PLAN_TIER"] = tier
        else:
            os.environ["AGENTIC_PLAN_TIER"] = "bridge"
            logger.error("plan_tier: persisted state %s malformed/invalid — forcing bridge", path)


def plan_tier_state() -> dict[str, object]:
    """Current tier + provenance for the GET surface. ``source`` is ``operator``
    when a persisted UI value is in force, else ``env-default``. The file read is
    taken under the flip lock so it sees a consistent snapshot vs a concurrent
    ``set_plan_tier``."""
    path = _plan_tier_state_path()
    source = "env-default"
    set_by: object = None
    set_at: object = None
    with _plan_tier_lock:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if str(raw.get("tier", "")).strip().lower() in ("bridge", "enterprise"):
                source = "operator"
                set_by = raw.get("set_by")
                set_at = raw.get("set_at")
        except (FileNotFoundError, ValueError, OSError):
            pass
    return {"tier": plan_tier(), "source": source, "set_by": set_by, "set_at": set_at}


def pick_lane(task_type: TaskType, sensitivity: Sensitivity) -> Lane:
    """Return the lane name to dispatch to.

    Conservative defaults: when in doubt, route more restrictively (local).
    """
    tier = plan_tier()

    # ---------------------------------------------------------------------
    # Task-type rules that beat sensitivity (always force a specific lane
    # regardless of how the material is classified).
    # ---------------------------------------------------------------------

    # Transcript extraction: always local. Verbatim transcripts often contain
    # things that would re-classify mid-call (a casual mention of a deal name
    # turns a "public" call confidential). Conservative — always Ollama.
    if task_type == "transcript-extraction":
        return "ollama"

    # Embeddings: always local. We have nomic-embed-text; no point going to cloud.
    if task_type == "embed":
        return "ollama-embed"

    # Multimodal extraction: always local. Cloud Claude has vision but we lean
    # on the local lane for inbound docs (teasers, CIMs) that may carry NDA
    # or restricted-list material before sensitivity has been triaged.
    if task_type == "multimodal-extraction":
        return "ollama-multimodal"

    # ---------------------------------------------------------------------
    # Sensitivity-driven rules.
    # ---------------------------------------------------------------------

    # MNPI: always local, regardless of tier
    if sensitivity == "MNPI":
        if task_type in ("triage", "classification", "news-bulk"):
            return "ollama-haiku"
        return "ollama"

    # Confidential: local during bridge; enterprise routes to cloud Claude (with ZDR)
    if sensitivity == "confidential":
        if tier == "enterprise":
            if task_type in ("triage", "classification", "news-bulk"):
                return "claude-cli-haiku"
            if task_type == "cross-check":
                return "codex-cli"  # ChatGPT Enterprise has its own retention controls
            return "claude-cli"
        # Bridge phase
        if task_type in ("triage", "classification", "news-bulk"):
            return "ollama-haiku"
        return "ollama"

    # Generic format work on non-confidential, non-deal-named content -> M3 OK
    if task_type in ("format-word", "format-template-generic", "tone-polish-generic") \
       and sensitivity in ("public", "internal"):
        return "minimax"

    # News-bulk / triage / classification on public/internal -> Haiku-grade
    if task_type in ("triage", "classification", "news-bulk"):
        return "claude-cli-haiku"

    # Cross-check on public/internal -> Codex
    if task_type == "cross-check" and sensitivity in ("public", "internal"):
        return "codex-cli"

    # Default safe lane: cloud Claude default model
    return "claude-cli"


def enterprise_cloud_lane(task_type: TaskType) -> Lane | None:
    """The cloud lane the ENTERPRISE tier grants a sensitive call for a given
    ``task_type`` — the single source of that mapping, shared by:

      * the confidential→cloud operator override window
        (``override_cloud_lane`` below), and
      * the P5 enterprise-MNPI attestation lift
        (``mnpi_attestations.mnpi_cloud_lane_if_attested``),

    so all the sanctioned cloud lifts resolve to IDENTICAL lanes. It mirrors the
    inline ``confidential``+enterprise mapping in ``pick_lane`` above (triage /
    classification / news-bulk → Haiku; cross-check → Codex; else Opus).

    Task-type rules that force local ALWAYS (transcript-extraction, embed,
    multimodal-extraction) are NOT lifted — a sensitivity escape hatch is not a
    task-type one. Returns ``None`` for those: callers must stay local.
    """
    if task_type in ("transcript-extraction", "embed", "multimodal-extraction"):
        return None
    if task_type in ("triage", "classification", "news-bulk"):
        return "claude-cli-haiku"
    if task_type == "cross-check":
        return "codex-cli"
    return "claude-cli"


def override_cloud_lane(task_type: TaskType) -> Lane | None:
    """The cloud lane a #llm-routing-override window lifts a CONFIDENTIAL call to
    — deliberately the SAME lane the enterprise tier grants (delegates to
    ``enterprise_cloud_lane``), so an operator override window means "enterprise
    routing for this window", nothing looser. Returns ``None`` for the
    always-local task types.

    MNPI never flows through THIS helper — the confidential override path gates
    on ``confidential``. The enterprise-MNPI attestation lift calls
    ``enterprise_cloud_lane`` directly (#llm-routing-postjune15 P5); MNPI is never
    liftable by an override window.
    """
    return enterprise_cloud_lane(task_type)


def lane_to_model(lane: Lane) -> tuple[str, str]:
    """Map a lane to (provider, model_name) for use by Ollama client / Claude CLI.

    For local lanes, returns the Ollama model. For cloud, returns the alias the
    CLI expects.
    """
    mapping: dict[Lane, tuple[str, str]] = {
        "ollama":              ("ollama", "qwen3:14b"),
        "ollama-haiku":         ("ollama", "qwen3:8b"),
        "ollama-embed":         ("ollama", "nomic-embed-text"),
        "ollama-multimodal":    ("ollama", "gemma4:e4b"),
        "claude-cli":          ("claude", "opus"),       # default Opus 4.8 (short name; client _model_alias pins the id)
        "claude-cli-haiku":     ("claude", "haiku"),      # Haiku 4.5
        "codex-cli":           ("codex", "gpt-5"),       # default Codex model alias
        "minimax":             ("minimax", "MiniMax-M3"),
    }
    return mapping[lane]


# ── Task-class provider bias (#llm-routing-postjune15 P2 §B) ─────────────────
# When a cloud-eligible skill expresses NO provider preference (no operator
# sidecar / SKILL.md frontmatter / AGENTIC_CLOUD_PROVIDER env), bias the PROVIDER
# by the KIND of work: analytical/heavy/cross-check → Codex (openai). Task
# classes NOT listed here have no specific bias and fall through to the caller's
# generic default (anthropic, the safe cloud default). This is the
# *provider-bias* layer Tier-2 consults; ``pick_lane`` above still owns the LANE
# (it already routes cross-check→codex-cli) — the two agree by construction, and
# making the provider choice explicit here stops the blind anthropic default
# from contradicting pick_lane's cross-check→codex intent. Operator prefs (and an
# explicit allow-list) override this bias. ``cross-check`` is the sole
# analytical/heavy CLOUD task type today; future analytical types join the
# ``openai`` bucket here.
_TASK_CLASS_DEFAULT_PROVIDER: dict[TaskType, str] = {
    "cross-check": "openai",
}


def task_class_provider_override(task_type: "TaskType | None") -> "str | None":
    """The cloud provider a task class biases toward when a skill expresses no
    preference (#llm-routing-postjune15 P2 §B), or ``None`` when the class has no
    specific bias (the caller then keeps its own ``default``). Unmapped /
    ``None`` → ``None``."""
    if task_type is None:
        return None
    return _TASK_CLASS_DEFAULT_PROVIDER.get(task_type)


# ── Operator model-level selection (#llm-routing-postjune15 P2 Task 3 + P4) ───
# The cloud model aliases an operator may pin per skill (``preferred_model``),
# beyond just the provider: the CURRENT Claude family aliases + the 1M-context
# variant. A ``-1m`` suffix marks the 1M-context variant. P2 Task 3 shipped the
# SELECTION + threading into ``chat(model=…)`` + the 1M context-window sizing
# with these aliases; the CONCRETE model-id pins landed in P4 (below).
#
# P4 (#llm-routing-postjune15): all four aliases now resolve to concrete current-
# generation ids in the client alias maps — ``opus`` → Opus 4.8, ``sonnet`` →
# Sonnet 4.6, ``haiku`` → Haiku 4.5, ``opus-1m`` → Opus 4.8 + the 1M window (CLI
# ``claude-opus-4-8[1m]`` / API native ``claude-opus-4-8``). This layer keeps the
# lane→SHORT-NAME mapping as the single source of the short name; the concrete id
# is resolved ONCE, in each client's ``_model_alias`` (not hardcoded here), and
# the ``cost_table`` short-name aliases price the same rows. ``opus-1m`` /
# ``sonnet`` now dispatch real calls instead of the pre-P4 graceful
# ``ERROR · CLAUDE SUBPROCESS`` reject of an unmapped id.
CLOUD_MODEL_ALIASES: tuple[str, ...] = ("opus", "sonnet", "haiku", "opus-1m")
ONE_M_CONTEXT_WINDOW = 1_000_000


def is_one_million_context_model(model: "str | None") -> bool:
    """True if ``model`` is a 1M-context variant — by the ``-1m`` alias suffix
    (#llm-routing-postjune15 P2 Task 3). The denominator for the chat header's
    "% context window" gauge then reads 1M instead of the default 200k.

    The match is lexical; it is safe only because the input is constrained to
    ``CLOUD_MODEL_ALIASES`` (boot + resolution-time validation), where the sole
    ``-1m`` member IS the 1M variant. It keys on the ANTON alias, NOT the
    concrete dispatch id — so it reads 1M for both encodings P4 pins (CLI
    ``claude-opus-4-8[1m]`` / API native ``claude-opus-4-8``). Preserve the
    invariant: don't introduce an unrelated alias ending ``-1m``."""
    return bool(model) and model.lower().endswith("-1m")
