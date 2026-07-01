"""F-27 — restrict ``state/`` to the operator at the filesystem ACL layer.

``state/`` holds the Fernet-encrypted credential store plus its
DPAPI-wrapped key. DPAPI already protects CONFIDENTIALITY against another
user account, but the default directory ACLs let any authenticated local
user delete/replace the blobs (an availability / substitution surface).
This module tightens the directory to: current user + SYSTEM +
Administrators, inheritance removed — applied idempotently at bridge
startup (the operator-gated restart IS the deploy gate).

Best-effort by design: ACL plumbing must never kill the boot. Failures log
a warning and return ``False``; the security posture then matches today's
(DPAPI-only), never worse.

The DACL is REPLACED wholesale via ``Set-Acl`` (codex SEV-3 round: an
``icacls /grant:r`` only replaces ACEs for the NAMED principals — explicit
pre-existing grants to Users / Everyone / a planted local account would
survive). After this runs, exactly three ACEs exist: the current process
user (by SID, taken from the live token — no name resolution), SYSTEM
(``S-1-5-18``) and Administrators (``S-1-5-32-544``), inheritance off.
"""

from __future__ import annotations

import logging
import platform
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_SYSTEM_SID = "S-1-5-18"
_ADMINISTRATORS_SID = "S-1-5-32-544"

# Replace the directory's DACL with exactly: current-user + SYSTEM +
# Administrators, FullControl, inherited by children; inheritance severed.
# RemoveAccessRule on a copy of .Access strips EVERY pre-existing explicit
# ACE — including ones icacls /grant:r would have left behind.
_PS_TEMPLATE = r"""
$ErrorActionPreference = 'Stop'
$dir = {dir}
$acl = Get-Acl -Path $dir
$acl.SetAccessRuleProtection($true, $false)
foreach ($rule in @($acl.Access)) {{ [void]$acl.RemoveAccessRule($rule) }}
$sids = @(
    [System.Security.Principal.WindowsIdentity]::GetCurrent().User,
    (New-Object System.Security.Principal.SecurityIdentifier('{system_sid}')),
    (New-Object System.Security.Principal.SecurityIdentifier('{admins_sid}'))
)
foreach ($sid in $sids) {{
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
        $sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    [void]$acl.AddAccessRule($rule)
}}
Set-Acl -Path $dir -AclObject $acl
"""


def _build_ps_script(state_dir: Path) -> str:
    # Single-quoted PowerShell literal; embedded quotes doubled.
    quoted = "'" + str(state_dir).replace("'", "''") + "'"
    return _PS_TEMPLATE.format(
        dir=quoted, system_sid=_SYSTEM_SID, admins_sid=_ADMINISTRATORS_SID,
    )


def harden_state_dir_acl(state_dir: Path | None = None) -> bool:
    """Replace ``state/``'s DACL with the operator-only set. True on success.

    Windows-only (the bridge's production host); a non-Windows platform or
    any failure is a logged no-op so startup never depends on ACL plumbing
    succeeding."""
    if platform.system() != "Windows":
        return False
    try:
        if state_dir is None:
            from routines.credentials.dpapi_key import _default_state_dir
            state_dir = _default_state_dir()
        state_dir.mkdir(parents=True, exist_ok=True)

        proc = subprocess.run(
            [
                "powershell", "-NoProfile", "-NonInteractive",
                "-Command", _build_ps_script(state_dir),
            ],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            logger.warning(
                "state ACL hardening failed (rc=%d): %s",
                proc.returncode, (proc.stderr or proc.stdout).strip()[:300],
            )
            return False
        logger.info(
            "state/ DACL replaced: current user + SYSTEM + Administrators (%s)",
            state_dir,
        )
        return True
    except Exception as e:  # noqa: BLE001 — ACL plumbing never kills boot
        logger.warning("state ACL hardening skipped: %s", e)
        return False


__all__ = ["harden_state_dir_acl"]
