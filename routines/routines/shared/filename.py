"""Filename-safe sanitisation helpers.

Used by skill writers (comps, equity-research, intake, ...) when composing
filenames from operator-provided or provider-returned strings that may
contain characters Windows refuses (``< > : " / \\ | ? *`` + control
chars 0x00-0x1f) or that would corrupt the final name (trailing dots /
spaces — Windows silently strips them on file create, surprising
behaviour for the operator).

Why this module exists: every skill that wrote files used to roll its
own regex (``_safe_name`` in comps + equity_research, etc.). When the
operator pulled comps for ``Apple Inc.``, the trailing dot survived the
per-skill regex and the final path landed as ``Companies/Apple-Inc..md``
— double-dot before the extension. Single helper, single source of
truth.

See [[workspace-write-policy]] for the broader file-system convention.
"""

from __future__ import annotations

import re


# Windows-illegal filename characters + control chars 0x00-0x1f.
# NOT the same as the broader "URL-safe" pattern — we deliberately preserve
# spaces, hyphens, and parentheses because they're operator-readable.
_FILENAME_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')

# Collapse runs of dots / spaces / underscores / hyphens to a single dash
# so ``Apple Inc..`` and ``Apple-Inc---`` both yield ``Apple-Inc``.
_FILENAME_RUNS = re.compile(r'[ ._-]+')


def sanitize_filename_component(name: str, *, max_len: int = 200) -> str:
    """Strip illegal chars, collapse separators, trim, cap length.

    The result is safe for use as ONE component of a Windows path
    (filename or directory name). Does NOT add an extension.

    Edge cases:
      * Empty input → empty string. Caller is responsible for falling back
        to a deterministic alternative (e.g. ticker) so a write never lands
        with a zero-length filename component.
      * All-illegal input (e.g. ``"<<<>>>"``) → empty string after strip.
        Same fallback contract.
      * Trailing dots / spaces → stripped. (``"Apple Inc."`` → ``"Apple-Inc"``.)
      * Non-string input → empty string. Defensive against provider data
        that returns ``None`` or a numeric symbol.
    """
    if not isinstance(name, str):
        return ""
    cleaned = _FILENAME_BAD.sub("", name)
    # Collapse internal separator runs to a single dash. Then trim any
    # leading/trailing separator (dash now subsumes dot/space/underscore).
    cleaned = _FILENAME_RUNS.sub("-", cleaned).strip("-").strip()
    if not cleaned:
        return ""
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip("-")
    return cleaned


def safe_filename(name: str, *, fallback: str, max_len: int = 200) -> str:
    """Sanitize ``name``; fall back to ``fallback`` (also sanitized) if
    the primary input yields an empty result.

    Convenience for the common skill pattern ``target_name or target_symbol``
    — ensures we never compose a path with an empty filename component."""
    primary = sanitize_filename_component(name, max_len=max_len)
    if primary:
        return primary
    return sanitize_filename_component(fallback, max_len=max_len)


__all__ = ["sanitize_filename_component", "safe_filename"]
