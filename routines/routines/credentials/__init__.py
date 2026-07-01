"""Encrypted-at-rest credentials store (#25).

Single-user variant of AutoGPT's ``IntegrationCredentialsManager`` (see
AUTOGPT-EVALUATION.md §2.3). Three credential kinds (``api_key`` /
``oauth2`` / ``user_password``) persisted in one Fernet-encrypted JSON
file at ``<routines-repo>/state/credentials.enc``. The Fernet key itself
is wrapped via Windows DPAPI so the file is bound to the operator's user
account — useless if copied to another machine.

Per-provider ``asyncio.Lock`` serialises OAuth refresh races without
pulling in Redis (AutoGPT uses Redis for the distributed case; we are
single-user, in-process).

Modules:
  * ``dpapi_key``    — get_or_create the Fernet key; DPAPI wrap/unwrap
  * ``store``        — CredentialsStore (CRUD; encrypted-at-rest)
  * ``lock_manager`` — per-provider asyncio.Lock dispenser
  * ``refresh``      — OAuth2 refresh skeleton (provider-specific impl
                       lands with #17 MS Graph)

Bridge route: ``routines/api/routes/credentials.py``. Contract:
OUTSTANDING.md ## CONTRACTS · credentials manager (#25).
"""

from routines.credentials.lock_manager import CredentialLockManager, get_lock_manager
from routines.credentials.store import (
    APIKeyCredential,
    CredentialKind,
    CredentialSummary,
    CredentialsStore,
    OAuth2Credential,
    StoredCredential,
    UserPasswordCredential,
    get_store,
)

__all__ = [
    "APIKeyCredential",
    "CredentialKind",
    "CredentialLockManager",
    "CredentialSummary",
    "CredentialsStore",
    "OAuth2Credential",
    "StoredCredential",
    "UserPasswordCredential",
    "get_lock_manager",
    "get_store",
]
