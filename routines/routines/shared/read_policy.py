"""Central read policy — the read-side counterpart of ``write_policy``.

#sec-read-path-policy (Shannon DAST run 2026-06-12; codex-adjudicated
DEFENSE-IN-DEPTH under the loopback-bind + F-1 CSRF-middleware posture):
every bridge WRITE passes through ``write_policy.ensure_write_allowed``
(the F-4 chokepoint), but the pdf-intake READ path had no equivalent —
``POST /api/workflows/pdf-intake`` would open ANY ``path`` the request
named and return the extracted content in the HTTP response. This module
closes that read/write asymmetry.

The read surface is deliberately WIDER than the write sandbox: pdf-intake
is designed for operator-pointed documents (CIMs / teasers / filings) that
live outside the vault. The policy is therefore "no traversal / UNC /
device tricks, and inside a KNOWN document root" — not "vault only":

* the configured vault (``routines.api.deps.VAULT``), read lazily at call
  time exactly like the write side, so a relocated vault and tmp-vault
  tests enforce the same shape;
* the static document roots below — the Corporate Finance research drive
  (mirroring ``write_policy.STATIC_WRITE_ROOTS``) and the project
  workspace root the skills' ``fs_roots`` capability declares
  (routines CLAUDE.md §14.7), in both Windows and WSL shapes;
* operator extensions via ``AGENTIC_INTAKE_READ_ROOTS`` —
  ``os.pathsep``-separated ABSOLUTE roots (e.g. a Downloads folder),
  honoured without a code change.

The lexical hardening is the SAME as the write side — the helpers are
imported from ``write_policy``, not copied, so a hardening fix there is
automatically load-bearing here: lowercase + slash normalisation, Win32
trailing dot/space stripped per component, ``..`` collapsed BEFORE the
prefix check (``posixpath.normpath``), UNC / device / extended-length
namespaces refused, NTFS ADS colons refused, reserved DOS device names
refused, and reparse points (junctions/symlinks) below a matched root
refused — a junction inside a read root would otherwise redirect the
read outside it.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Sequence
from pathlib import Path

# Shared lexical hardening — the SAME primitives the F-4 write chokepoint
# uses. Module-private there by convention, but this is their second
# in-package consumer, not an external import.
from routines.shared.write_policy import (
    _configured_vault_root,
    _has_ads_colon,
    _has_reserved_device,
    _normalise,
    _reparse_below_anchor,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ReadPolicyViolation",
    "READ_ROOTS_ENV",
    "STATIC_READ_ROOTS",
    "default_read_roots",
    "read_path_is_allowed",
    "ensure_read_allowed",
]


class ReadPolicyViolation(RuntimeError):
    """Raised when a read would land outside the allowed read roots."""


# Operator extension knob: os.pathsep-separated ABSOLUTE roots (e.g.
# ``C:\\Users\\<me>\\Downloads``). Read at call time — no code change and
# no module reload needed, only the bridge process env.
READ_ROOTS_ENV = "AGENTIC_INTAKE_READ_ROOTS"

# Static absolute document roots (lowercase, trailing slash; Windows + WSL
# shapes, matching the STATIC_WRITE_ROOTS conventions). The CF drive
# mirrors the write side's root; ``<workspace-root>/`` is the project workspace
# root (LBO's Output Contract / §14.7 fs_roots) where deal documents land.
STATIC_READ_ROOTS = (
    "<workspace-root>/",
    "<workspace-root>/",
    "/mnt/x/corporate finance/",
    "/mnt/x/projects/",
)

# A usable root must normalise to an ABSOLUTE local path — a drive-letter
# shape ("x:/…") or a POSIX absolute ("/mnt/x/…"). Anything else (a
# relative env entry, an empty string) anchors nothing.
_ABS_NORM_RE = re.compile(r"^(?:[a-z]:/|/)")


def _env_read_roots() -> list[str]:
    """Operator-extended roots from ``AGENTIC_INTAKE_READ_ROOTS``.

    Entries that fail the shared normalisation or are not absolute are
    SKIPPED with a warning — one bad entry must neither widen the policy
    nor break the good ones."""
    raw = os.environ.get(READ_ROOTS_ENV, "")
    roots: list[str] = []
    for entry in raw.split(os.pathsep):
        entry = entry.strip()
        if not entry:
            continue
        norm = _normalise(entry)
        if norm is None or not _ABS_NORM_RE.match(norm):
            logger.warning(
                "%s entry %r ignored — not a plain absolute local path",
                READ_ROOTS_ENV, entry,
            )
            continue
        roots.append(norm.rstrip("/") + "/")
    return roots


def default_read_roots() -> tuple[str, ...]:
    """The legitimate intake document roots, normalised with a trailing
    slash: configured vault + static document roots + env extensions.
    Computed at CALL time — the vault may be monkeypatched in tests and
    the env knob is live."""
    roots: list[str] = []
    vault = _configured_vault_root()
    if vault is not None:
        roots.append(vault.rstrip("/") + "/")
    roots.extend(STATIC_READ_ROOTS)
    roots.extend(_env_read_roots())
    return tuple(roots)


def read_path_is_allowed(
    path: str | Path, *, read_roots: Sequence[str | Path] | None = None
) -> bool:
    """True if ``path`` may be read.

    ``read_roots`` overrides the defaults (tests pass ``[tmp_path]``);
    entries go through the same normalisation as paths and an unparseable
    or relative entry contributes nothing (fail-closed: fewer allowed
    paths, never more). Matching is lexical-first — ``..`` is collapsed
    before any prefix comparison and refused paths never touch the
    filesystem (no UNC dial-out, no existence oracle)."""
    norm = _normalise(str(path))
    if norm is None:
        return False
    if _has_ads_colon(norm) or _has_reserved_device(norm):
        return False

    candidates: Sequence[str | Path] = (
        default_read_roots() if read_roots is None else read_roots
    )
    for root in candidates:
        r = _normalise(str(root))
        if r is None or not _ABS_NORM_RE.match(r):
            continue
        r = r.rstrip("/")
        if not r:
            continue
        if norm == r or norm.startswith(r + "/"):
            # Same junction refusal as the write side: a reparse point
            # below the matched root would redirect the read outside it.
            return not _reparse_below_anchor(norm, r)
    return False


def ensure_read_allowed(
    target: str | Path,
    *,
    read_roots: Sequence[str | Path] | None = None,
    op: str = "read",
) -> None:
    """Raise :class:`ReadPolicyViolation` unless ``target`` is allowed.

    Call BEFORE any filesystem access on a caller-supplied path — even
    ``is_file()`` on an attacker-shaped UNC path dials out to the share."""
    if not read_path_is_allowed(target, read_roots=read_roots):
        raise ReadPolicyViolation(
            f"refused {op} of {str(target)!r} — outside the allowed read "
            f"roots (#sec-read-path-policy; operator can extend via "
            f"{READ_ROOTS_ENV})"
        )
