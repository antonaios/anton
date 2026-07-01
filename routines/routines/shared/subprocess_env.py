"""Minimal subprocess environment for CLI-agent children (F-5 / HR S-5).

The ``claude -p`` and ``codex exec`` subprocesses used to inherit the FULL
bridge environment — including every provider API key in it
(``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` / ``FIRECRAWL_API_KEY`` /
``TAVILY_API_KEY`` / ``FRED_API_KEY`` …). A compromised or over-permissive CLI
child (``codex exec`` is an *agent* that can run local tools) would then see
every key. The crew lane already allowlists its child env (``crew/proxy
._child_env``); these two clients were the asymmetric gap.

This builds an explicit, minimal ``env=`` that keeps only:
  * OS process plumbing (mirrors ``crew/proxy._child_env``), and
  * the profile / config-home vars where ``claude login`` / ``codex login``
    store their OAuth tokens,
and DROPS everything else — so the provider API keys never cross into the
child.

⚠ The subprocess lanes authenticate via OAuth / plan-credit (``claude login``
→ ~/.claude, ``codex login`` → ~/.codex), NOT via the API keys, so dropping the
keys does NOT break them. A PRIOR over-strip (2026-06-03) — which redirected
``CODEX_HOME`` to a scratch dir AND used a too-narrow allowlist — hid the
operator's OAuth login and broke the OpenAI lane completely. The lesson is
baked into the allowlist below: it is deliberately GENEROUS about
profile / config-home / TLS-trust vars; only secrets are dropped. **Re-verify
``codex login status`` after any change here.**
"""

from __future__ import annotations

import os
from typing import Iterable, Optional


# OS plumbing (mirrors crew/proxy._child_env) + the profile / config-home / TLS
# vars the node-based CLIs and their OAuth flows need. NOTHING provider-secret
# is listed — an env var absent from this set is dropped from the child.
_ENV_ALLOWLIST = frozenset({
    # ── Windows process plumbing ──
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PATH", "SYSTEMDRIVE",
    "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES",
    "HOMEDRIVE", "HOMEPATH", "USERNAME", "USERDOMAIN", "COMPUTERNAME",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "OS",
    # ── POSIX equivalents (dev boxes / CI) ──
    "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "USER", "LOGNAME", "SHELL",
    # ── TLS trust roots (so HTTPS to the provider works from the child) ──
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "CURL_CA_BUNDLE",
    # ── CLI OAuth / config homes — where `claude login` / `codex login` keep
    #    their auth.json. MISSING ONE OF THESE is exactly what broke the lane
    #    before, so the allowlist is generous here. ──
    "CODEX_HOME",
    "CLAUDE_CONFIG_DIR", "CLAUDE_HOME",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
    "XDG_RUNTIME_DIR",
    # NOTE: ``NODE_OPTIONS`` / ``NODE_PATH`` are DELIBERATELY NOT allowlisted
    # (codex-5.5 F-5 r1, High). For a node-based CLI these are code-loading /
    # module-resolution controls — ``NODE_OPTIONS=--require evil.js`` would
    # execute attacker-controlled JS *before* ``codex``/``claude`` starts,
    # defeating the hardened env. They are not OAuth-critical (login verified
    # without them). The CLI binary itself is resolved to an ABSOLUTE path in
    # the parent (``_resolve_bin_path``) so the kept ``PATH`` selects the
    # child's own dependencies, not the main binary.
})


def minimal_cli_env(
    *,
    extra_keep: Optional[Iterable[str]] = None,
    base: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Return a minimal environment for a CLI-agent subprocess (F-5).

    Keeps only the allowlisted OS-plumbing + OAuth/config-home vars from
    ``base`` (defaults to ``os.environ``); every other var — crucially the
    provider API keys — is dropped. Matching is case-insensitive on Windows
    (env var names are case-insensitive there) but preserves the original key
    casing in the returned dict.

    Args:
        extra_keep: additional env-var names to allow through (e.g. a binary's
            own ``AGENTIC_*_CLI_PATH``). Case-insensitive.
        base: the source environment to filter; defaults to ``os.environ``.
    """
    src = dict(os.environ if base is None else base)
    allow = {k.upper() for k in _ENV_ALLOWLIST}
    if extra_keep:
        allow |= {k.upper() for k in extra_keep}
    return {k: v for k, v in src.items() if k.upper() in allow}


__all__ = ["minimal_cli_env", "_ENV_ALLOWLIST"]
