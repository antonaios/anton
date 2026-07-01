"""Central write policy — the ONE allowlist every bridge write passes through.

F-4 ``#sec-workspace-policy-chokepoint`` (SEV-2, 2026-06-11 review): the
"central" workspace policy used to fire only for 4 LLM tool names — no HTTP
route write passed through it, so containment rested on each route's own
validators (the root cause behind F-2 and any future write route). This module
makes the allowlist load-bearing for EVERY writer:

* ``atomic_write`` / ``atomic_move`` (``routines.shared.vault_writer``) call
  :func:`ensure_write_allowed` fail-closed BEFORE any side effect;
* the LLM tool-call guard (``central_guards.enforce_workspace_policy``) and
  the deal-tracker workbook sandbox (F-2) consult the same
  :func:`path_is_allowed`.

Policy shape (see [[workspace-write-policy]] §2):

* **Static absolute roots** — non-vault write surfaces (the routines repo
  ``runs/``, the umbrella ``sessions/``, the Corporate Finance research
  drive), plus the legacy vault ``Topics/`` / ``Routines/`` entries kept for
  back-compat with the pre-F-4 policy.
* **Vault-anchored prefixes** — the real write sandbox observed in the
  2026-06-11 full write-target inventory (every ``atomic_write`` caller):
  routed notes, proposals, registers, stubs and views. Anchored on the
  CONFIGURED vault root (``routines.api.deps.VAULT``) or an explicit
  ``vault_root=`` the writer passes — never on hardcoded drive paths — so a
  relocated vault (``AGENTIC_VAULT``) and tmp-vault tests enforce the same
  shape.
* **Exact-file allows** — operatorconfig's four ``_claude/`` config notes.
  The ``_claude/`` tree as a whole is NOT writable: it also holds the
  constitution redirect stub, the constitution manifest and operator session
  files (MEMORY.md / WIP.md / HEARTBEAT.md) the bridge has no business
  touching.
* **Constitution deny-set** — the #claudemd-restructure constitution files
  are REFUSED even where they sit inside an allowed root
  (``Projects/CLAUDE.md`` under the necessarily-broad ``Projects/`` prefix).
  This is the §1.4 handshake with the restructure session made mechanical:
  the bridge cannot write any constitution-set file, full stop. The repo-side
  CLAUDE.md files (``<repo>/CLAUDE.md``, ``<repo>/CLAUDE.md``,
  ``<repo>/routines/CLAUDE.md``) sit outside every allowed root.

Hardening carried over from the F-2 sandbox (deal-tracker, SEV-1): UNC /
device / extended-length namespaces refused outright; NTFS alternate-data-
stream colons refused; reserved DOS device names (``NUL.md`` / ``COM1.xlsx``)
refused in any component. Matching is lexical (``posixpath.normpath`` after
lowercase + slash normalisation — no filesystem access) with ``..``
collapsed BEFORE the anchored prefix check, identical to the audited
``_path_is_allowed`` semantics this module absorbs.
"""

from __future__ import annotations

import logging
import os
import posixpath
import stat as stat_mod
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "WorkspacePolicyViolation",
    "STATIC_WRITE_ROOTS",
    "VAULT_WRITE_PREFIXES",
    "VAULT_WRITE_FILES",
    "VAULT_DENY_FILES",
    "WIN_RESERVED_DEVICE_NAMES",
    "path_is_allowed",
    "ensure_write_allowed",
]


class WorkspacePolicyViolation(RuntimeError):
    """Raised when a write would land outside the allowed paths.

    Canonical definition (moved here from ``hooks.central_guards`` with F-4 —
    that module re-exports it, so existing ``from routines.hooks.central_guards
    import WorkspacePolicyViolation`` imports keep resolving the SAME class)."""


# Static absolute roots per [[workspace-write-policy]] §2. The bridge runs on
# Windows (X:\) and WSL (/mnt/x); the policy applies to either path shape.
# Roots and incoming paths are normalised to lowercase + forward slashes
# before comparison. The two vault entries are the pre-F-4 legacy policy —
# they stay so the tool-call guard's contract is unchanged; the full vault
# sandbox is the ANCHORED prefix set below.
STATIC_WRITE_ROOTS = (
    "<vault>/topics/",
    "<vault>/routines/",
    "<repo>/routines/runs/",
    "<repo>/sessions/",
    "<workspace-root>/",
    "/mnt/x/os ai vault/topics/",
    "/mnt/x/os ai vault/routines/",
    "/mnt/x/agentic os/routines/runs/",
    "/mnt/x/agentic os/sessions/",
    "/mnt/x/corporate finance/",
)

# Vault-relative prefixes (lowercase, trailing slash) — the real write sandbox
# from the 2026-06-11 atomic_write call-site inventory. Notably ABSENT, on
# purpose: ``Daily/`` (operator-written, no bridge writer), ``Templates/``,
# ``Archive/`` ([no-archive-writes]), ``_claude/`` (exact files only, below),
# and the vault root itself (where the constitution lives).
VAULT_WRITE_PREFIXES = (
    "topics/",
    "routines/",
    "inbox/",
    "resources/",
    "projects/",
    "companies/",
    "people/",
    "sectors/",
    "registers/",
    "_processing/",
)

# Exact vault-relative files (lowercase) — operatorconfig's SECTION_FILES map.
VAULT_WRITE_FILES = frozenset({
    "_claude/tickers.md",
    "_claude/earnings-watchlist.md",
    "_claude/news-coverage.md",
    "_claude/profile.md",
})

# Constitution set (#claudemd-restructure) — DENIED even under an allowed
# prefix. Mirrors ``_claude/constitution-manifest.json`` ``expected_files``
# plus the manifest itself (the hash pins). ``claude.md`` (vault root) and
# ``templates/claude.md`` are outside every prefix anyway — listed for
# belt-and-braces and so the §1.4 regression test reads as the full set.
VAULT_DENY_FILES = frozenset({
    "claude.md",
    "projects/claude.md",
    "templates/claude.md",
    "_claude/claude.md",
    "_claude/constitution-manifest.json",
})

# Reserved DOS device names — a write to ``NUL.md`` / ``COM1.xlsx`` goes to
# the device, not a file (F-2 r2 hardening, ported central).
WIN_RESERVED_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def _normalise(path: str) -> str | None:
    """Lowercase + forward slashes + Win32 trailing dot/space stripped per
    component + ``..``/``.`` collapsed, or ``None`` when the path is refused
    outright.

    Rejects UNC / device / extended-length namespaces (any leading ``//`` —
    ``\\\\server\\share``, ``\\\\?\\``, ``\\\\.\\``): the allowed roots are all
    plain LOCAL paths, and a UNC path's Windows interpretation diverges from
    posix ``..`` math, so ``normpath`` could reduce it to a local-looking
    allowed path while the real write escapes to a network share.

    Win32 silently strips trailing dots/spaces when opening each component, so
    ``Projects/CLAUDE.md.`` and ``Projects/CLAUDE.md `` open the REAL
    ``CLAUDE.md`` while comparing unequal to the deny-set (codex F-4 r1
    SEV-1). Strip per component BEFORE comparison — keeping ``.``/``..``
    verbatim so traversal collapse still sees them; a component that strips to
    empty (``...``) is malformed → refused."""
    n = path.lower().replace("\\", "/")
    if n.startswith("//"):
        return None
    parts = []
    for comp in n.split("/"):
        if comp in (".", ".."):
            parts.append(comp)
            continue
        stripped = comp.rstrip(" .")
        if comp and not stripped:
            return None  # dots/spaces-only component — malformed, refuse
        parts.append(stripped)
    return posixpath.normpath("/".join(parts))


def _has_ads_colon(norm: str) -> bool:
    """True when a ``:`` appears beyond the drive letter — NTFS alternate-
    data-stream syntax (``…/note.md:payload``): a hidden write primitive
    against an arbitrary existing file inside the sandbox."""
    rest = norm[2:] if len(norm) >= 2 and norm[1] == ":" else norm
    return ":" in rest


def _has_reserved_device(norm: str) -> bool:
    for part in norm.split("/"):
        stem = part.rstrip(" .").split(".", 1)[0].strip()
        if stem in WIN_RESERVED_DEVICE_NAMES:
            return True
    return False


def _is_reparse_point(p: Path) -> bool:
    """True when ``p`` exists and is a symlink / Windows reparse point
    (junction, mount point). ``lstat`` errors on an EXISTING path are treated
    as reparse (fail-closed); a missing path is fine — a directory that does
    not exist yet cannot be a junction (``atomic_write`` creates it fresh)."""
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return False
    except OSError:
        return True  # can't inspect an existing component → fail closed
    if stat_mod.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    reparse_flag = getattr(stat_mod, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attrs & reparse_flag)


def _reparse_below_anchor(norm: str, anchor: str) -> bool:
    """True when any component of ``norm`` STRICTLY BELOW ``anchor`` (the
    matched allow root, itself exempt) is a symlink/junction. The policy is
    otherwise lexical; without this, a junction planted under ``Projects/``
    redirects an allowed-looking write outside the sandbox (codex F-4 r1
    SEV-2). Only existing components can be reparse points, so fresh
    subtrees cost a couple of no-op lstats."""
    ar = anchor.rstrip("/")
    rel = norm[len(ar) + 1 :]
    cur = Path(ar)
    for comp in rel.split("/"):
        if not comp:
            continue
        cur = cur / comp
        if _is_reparse_point(cur):
            return True
    return False


def _configured_vault_root() -> str | None:
    """The bridge's configured vault root, normalised — read from
    ``routines.api.deps`` at CALL time (lazy import, so a monkeypatched
    ``deps.VAULT`` in tests is honoured and module import order doesn't
    matter). ``None`` when unresolvable → the vault-anchored layer simply
    contributes nothing (fail-closed: fewer allowed paths, never more)."""
    try:
        from routines.api import deps  # lazy: avoid import at module load
        raw = deps.VAULT
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    return _normalise(str(raw))


def path_is_allowed(path: str | Path, *, vault_root: str | Path | None = None) -> bool:
    """True if ``path`` may be written.

    ``vault_root`` is the OPTIONAL anchor the caller built the path from
    (every vault writer takes one as a parameter — pass it through). It must
    be server-config-derived (``deps.VAULT`` / ``VaultPaths.root``), NEVER
    caller/request input. The configured vault is always consulted as well,
    so omitting it only matters for tmp-vault tests. Fail-closed: anything
    that resolves under no anchor and no static root is refused."""
    norm = _normalise(str(path))
    if norm is None:
        return False
    if _has_ads_colon(norm) or _has_reserved_device(norm):
        return False

    # Anchor candidates: the caller's explicit vault root, then the
    # configured one. An unparseable explicit anchor fails closed.
    anchors: list[str] = []
    if vault_root is not None:
        a = _normalise(str(vault_root))
        if a is None:
            return False
        anchors.append(a)
    cfg = _configured_vault_root()
    if cfg is not None and cfg not in anchors:
        anchors.append(cfg)

    rel: str | None = None
    matched_anchor: str | None = None
    for anchor in anchors:
        ar = anchor.rstrip("/")
        if norm == ar:
            return False  # the vault root itself is never a write target
        if norm.startswith(ar + "/"):
            rel = norm[len(ar) + 1 :]
            matched_anchor = ar
            break

    if rel is not None and matched_anchor is not None:
        if rel in VAULT_DENY_FILES:
            return False  # constitution set — denied before ANY allow
        if rel in VAULT_WRITE_FILES or any(
            rel.startswith(p) for p in VAULT_WRITE_PREFIXES
        ):
            if _reparse_below_anchor(norm, matched_anchor):
                return False  # junction/symlink under the sandbox — refuse
            if cfg is not None and matched_anchor != cfg.rstrip("/"):
                # Authorized via a NON-configured anchor — by contract this
                # only happens in tmp-vault tests (anchors are server-config
                # derived). Debug telemetry so a misused anchor in bridge
                # code is observable without breaking the test architecture.
                logger.debug(
                    "write allowed via explicit non-configured vault anchor "
                    "%r (target %r)", matched_anchor, norm,
                )
            return True
        # Under the vault but outside the sandbox (Daily/, Templates/,
        # Archive/, root-level files…) → fall through to the static roots
        # (which cover the legacy vault Topics//Routines/ entries), else
        # refused.

    for root in STATIC_WRITE_ROOTS:
        r = root.rstrip("/")
        if norm == r or norm.startswith(r + "/"):
            return not _reparse_below_anchor(norm, r)
    return False


def ensure_write_allowed(
    target: str | Path, *, vault_root: str | Path | None = None, op: str = "write"
) -> None:
    """Raise :class:`WorkspacePolicyViolation` unless ``target`` is allowed.

    Called by ``atomic_write`` / ``atomic_move`` BEFORE any side effect
    (no parent mkdir, no tempfile) so a refused write leaves nothing behind."""
    if not path_is_allowed(target, vault_root=vault_root):
        raise WorkspacePolicyViolation(
            f"refused {op} to {str(target)!r} — outside the allowed write "
            "roots ([[workspace-write-policy]] §2 / F-4 central chokepoint)"
        )
