"""Encrypted-at-rest credentials store.

Implements OUTSTANDING.md ## CONTRACTS · credentials manager (#25).

On-disk layout:
  ``<routines-repo>/state/credentials.enc``       # Fernet-encrypted JSON
  ``<routines-repo>/state/credentials.key.dpapi`` # DPAPI-wrapped Fernet key

Both files are gitignored (broader ``state/`` rule + explicit lines).

In-memory cache: the encrypted file is loaded once on first read and held
in a process-local dict. Mutations re-encrypt and rewrite atomically (write
to ``.tmp``, ``rename`` over original).

Thread-safety: a single ``threading.Lock`` serialises all mutations. The
read path also acquires the lock briefly to safely copy state out — the
returned ``StoredCredential`` is a Pydantic model so callers can't mutate
the cache by accident.

NB: secret values **never** leave this module via wire-bound responses.
The bridge route uses ``list_summaries()`` (no secrets) for ``GET`` and
``CredentialSummary`` for ``POST``/``refresh``. ``get_credential()`` is
server-side only — used by future hook integration to resolve credentials
for outgoing LLM/tool calls.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from cryptography.fernet import InvalidToken
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from routines.credentials.dpapi_key import _default_state_dir, get_fernet

logger = logging.getLogger(__name__)


CredentialKind = Literal["api_key", "oauth2", "user_password"]


STORE_FILENAME = "credentials.enc"


def _store_path() -> Path:
    d = _default_state_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / STORE_FILENAME


# ────────────────────────────────────────────────────────────────────────────
# Credential payloads
# ────────────────────────────────────────────────────────────────────────────


class APIKeyCredential(BaseModel):
    """Single-token API key (Anthropic, OpenAI, FMP, MiniMax M2.7, etc.)."""
    model_config = ConfigDict(extra="forbid")

    kind: Literal["api_key"] = "api_key"
    provider: str = Field(..., min_length=1)
    api_key: SecretStr
    metadata: dict = Field(default_factory=dict)


class OAuth2Credential(BaseModel):
    """OAuth2 token bundle. ``expires_at`` is the *access_token* expiry —
    refresh fires when ``now`` ≥ ``expires_at``."""
    model_config = ConfigDict(extra="forbid")

    kind: Literal["oauth2"] = "oauth2"
    provider: str = Field(..., min_length=1)
    access_token: SecretStr
    refresh_token: SecretStr
    expires_at: str         # ISO-8601 UTC
    scopes: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class UserPasswordCredential(BaseModel):
    """Username + password (rare; reserved for legacy integrations)."""
    model_config = ConfigDict(extra="forbid")

    kind: Literal["user_password"] = "user_password"
    provider: str = Field(..., min_length=1)
    username: str = Field(..., min_length=1)
    password: SecretStr
    metadata: dict = Field(default_factory=dict)


# Discriminated union for the public store interface. The disk JSON carries
# ``kind`` as the discriminator; the union shape lets Pydantic pick the
# right model on round-trip.
StoredCredential = APIKeyCredential | OAuth2Credential | UserPasswordCredential


class CredentialSummary(BaseModel):
    """Wire-bound view — secret fields stripped. Returned by ``GET``."""
    model_config = ConfigDict(extra="forbid")

    provider: str
    kind: CredentialKind
    created: str                       # ISO-8601 UTC
    last_used: Optional[str] = None
    expires_at: Optional[str] = None   # OAuth2 only
    metadata: dict = Field(default_factory=dict)


# ────────────────────────────────────────────────────────────────────────────
# Disk format
# ────────────────────────────────────────────────────────────────────────────
#
# {
#   "providers": {
#     "anthropic": {"kind": "api_key", ..., "_meta": {"created": "...", "last_used": null}},
#     "ms-graph":  {"kind": "oauth2", ..., "_meta": {"created": "...", "last_used": null}}
#   }
# }
#
# The ``_meta`` block holds the lifecycle timestamps (created, last_used).
# It lives next to the credential fields rather than wrapping them so the
# secret payload's shape stays Pydantic-clean.


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _credential_from_disk(blob: dict) -> tuple[StoredCredential, dict]:
    """Split a disk row into ``(credential, meta)``. ``meta`` carries the
    lifecycle timestamps."""
    meta = blob.pop("_meta", {}) if isinstance(blob, dict) else {}
    kind = blob.get("kind")
    if kind == "api_key":
        cred: StoredCredential = APIKeyCredential.model_validate(blob)
    elif kind == "oauth2":
        cred = OAuth2Credential.model_validate(blob)
    elif kind == "user_password":
        cred = UserPasswordCredential.model_validate(blob)
    else:
        raise ValueError(f"unknown credential kind: {kind!r}")
    return cred, meta


def _credential_to_disk(cred: StoredCredential, meta: dict) -> dict:
    # SecretStr serialises to the actual secret value via ``model_dump`` only
    # with ``context={"reveal_secrets": True}`` — but the default in Pydantic v2
    # is to render as "**********". For on-disk encrypted storage we need the
    # real value, so dump via ``mode="json"`` after temporarily unwrapping.
    blob: dict = {
        "kind": cred.kind,
        "provider": cred.provider,
        "metadata": dict(cred.metadata),
    }
    if isinstance(cred, APIKeyCredential):
        blob["api_key"] = cred.api_key.get_secret_value()
    elif isinstance(cred, OAuth2Credential):
        blob["access_token"] = cred.access_token.get_secret_value()
        blob["refresh_token"] = cred.refresh_token.get_secret_value()
        blob["expires_at"] = cred.expires_at
        blob["scopes"] = list(cred.scopes)
    elif isinstance(cred, UserPasswordCredential):
        blob["username"] = cred.username
        blob["password"] = cred.password.get_secret_value()
    else:
        raise TypeError(f"unsupported credential type: {type(cred).__name__}")
    blob["_meta"] = dict(meta)
    return blob


# ────────────────────────────────────────────────────────────────────────────
# CredentialsStore
# ────────────────────────────────────────────────────────────────────────────


class CredentialsStore:
    """Encrypted JSON-on-disk store. One instance per process via
    ``get_store()``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: Optional[dict[str, tuple[StoredCredential, dict]]] = None
        # Resolved lazily so tests that monkeypatch the state dir env var
        # AFTER importing this module still pick up the right path.
        self._path: Optional[Path] = None

    # ── disk I/O ──────────────────────────────────────────────────────────

    def _resolve_path(self) -> Path:
        # Always resolve fresh: the env var may have been monkeypatched by
        # tests between instantiation and use.
        return _store_path()

    def _load_unlocked(self) -> dict[str, tuple[StoredCredential, dict]]:
        path = self._resolve_path()
        if not path.exists():
            return {}
        try:
            decrypted = get_fernet().decrypt(path.read_bytes())
        except InvalidToken as e:
            raise RuntimeError(
                f"credentials store decryption failed at {path} — Fernet key "
                f"mismatch or file corrupted. If you have a backup, restore "
                f"it; otherwise delete both ``credentials.enc`` and "
                f"``credentials.key.dpapi`` and re-add credentials. "
                f"Underlying: {e}"
            ) from e
        raw = json.loads(decrypted.decode("utf-8"))
        out: dict[str, tuple[StoredCredential, dict]] = {}
        for prov, blob in raw.get("providers", {}).items():
            cred, meta = _credential_from_disk(blob)
            out[prov] = (cred, meta)
        return out

    def _save_unlocked(
        self, cache: dict[str, tuple[StoredCredential, dict]],
    ) -> None:
        raw = {
            "providers": {
                prov: _credential_to_disk(cred, meta)
                for prov, (cred, meta) in cache.items()
            },
        }
        encrypted = get_fernet().encrypt(json.dumps(raw).encode("utf-8"))

        path = self._resolve_path()
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(encrypted)
        # ``os.replace`` is atomic on Windows + POSIX. Path.replace() wraps it.
        tmp.replace(path)

    def _ensure_cache(self) -> dict[str, tuple[StoredCredential, dict]]:
        if self._cache is None:
            self._cache = self._load_unlocked()
        return self._cache

    def reset_cache(self) -> None:
        """Drop the in-memory cache so the next call re-reads from disk.
        Tests use this when monkeypatching the state dir mid-test."""
        with self._lock:
            self._cache = None

    # ── public API ────────────────────────────────────────────────────────

    def list_summaries(self) -> list[CredentialSummary]:
        with self._lock:
            cache = self._ensure_cache()
            out: list[CredentialSummary] = []
            for cred, meta in cache.values():
                expires_at: Optional[str] = None
                if isinstance(cred, OAuth2Credential):
                    expires_at = cred.expires_at
                out.append(CredentialSummary(
                    provider=cred.provider,
                    kind=cred.kind,
                    created=meta.get("created", ""),
                    last_used=meta.get("last_used"),
                    expires_at=expires_at,
                    metadata=dict(cred.metadata),
                ))
        return sorted(out, key=lambda s: s.provider)

    def get_summary(self, provider: str) -> Optional[CredentialSummary]:
        for s in self.list_summaries():
            if s.provider == provider:
                return s
        return None

    def get_credential(self, provider: str) -> Optional[StoredCredential]:
        """Returns the full credential including secret data. SERVER-SIDE
        ONLY — never plumb this into a wire response."""
        with self._lock:
            cache = self._ensure_cache()
            entry = cache.get(provider)
            return entry[0] if entry else None

    def has(self, provider: str) -> bool:
        with self._lock:
            return provider in self._ensure_cache()

    def add(self, cred: StoredCredential) -> CredentialSummary:
        """Insert a new credential. Raises ``ValueError`` if provider already
        configured (caller maps to 409 at the bridge)."""
        with self._lock:
            cache = self._ensure_cache()
            if cred.provider in cache:
                raise ValueError(
                    f"provider {cred.provider!r} already configured; "
                    f"DELETE first to replace"
                )
            meta = {"created": _now_iso(), "last_used": None}
            cache[cred.provider] = (cred, meta)
            self._save_unlocked(cache)
            return self._summary_for_unlocked(cred, meta)

    def replace(self, cred: StoredCredential) -> CredentialSummary:
        """Upsert. Preserves ``created`` if the provider already exists;
        clears ``last_used``."""
        with self._lock:
            cache = self._ensure_cache()
            existing = cache.get(cred.provider)
            if existing is not None:
                meta = {"created": existing[1].get("created", _now_iso()), "last_used": None}
            else:
                meta = {"created": _now_iso(), "last_used": None}
            cache[cred.provider] = (cred, meta)
            self._save_unlocked(cache)
            return self._summary_for_unlocked(cred, meta)

    def remove(self, provider: str) -> bool:
        with self._lock:
            cache = self._ensure_cache()
            if provider not in cache:
                return False
            del cache[provider]
            self._save_unlocked(cache)
            return True

    def mark_used(self, provider: str) -> None:
        """Best-effort timestamp update. Swallows errors — never fails an
        LLM/tool call because we couldn't persist last_used."""
        try:
            with self._lock:
                cache = self._ensure_cache()
                entry = cache.get(provider)
                if entry is None:
                    return
                cred, meta = entry
                meta["last_used"] = _now_iso()
                cache[provider] = (cred, meta)
                self._save_unlocked(cache)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "credentials.mark_used failed for %s (ignored): %s",
                provider, e,
            )

    def update_oauth2_tokens(
        self,
        provider: str,
        *,
        access_token: str,
        refresh_token: Optional[str] = None,
        expires_at: str,
    ) -> CredentialSummary:
        """Apply an OAuth2 refresh result. Called by ``refresh.py`` after
        the upstream token endpoint returns. Preserves the refresh_token
        if the provider only rotates access tokens."""
        with self._lock:
            cache = self._ensure_cache()
            entry = cache.get(provider)
            if entry is None:
                raise KeyError(f"provider {provider!r} not configured")
            cred, meta = entry
            if not isinstance(cred, OAuth2Credential):
                raise TypeError(
                    f"provider {provider!r} is kind={cred.kind!r}; "
                    f"refresh only valid for kind=oauth2"
                )
            new_cred = OAuth2Credential(
                provider=provider,
                access_token=SecretStr(access_token),
                refresh_token=SecretStr(refresh_token) if refresh_token else cred.refresh_token,
                expires_at=expires_at,
                scopes=list(cred.scopes),
                metadata=dict(cred.metadata),
            )
            meta["last_used"] = _now_iso()
            cache[provider] = (new_cred, meta)
            self._save_unlocked(cache)
            return self._summary_for_unlocked(new_cred, meta)

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _summary_for_unlocked(
        cred: StoredCredential, meta: dict,
    ) -> CredentialSummary:
        expires_at: Optional[str] = None
        if isinstance(cred, OAuth2Credential):
            expires_at = cred.expires_at
        return CredentialSummary(
            provider=cred.provider,
            kind=cred.kind,
            created=meta.get("created", ""),
            last_used=meta.get("last_used"),
            expires_at=expires_at,
            metadata=dict(cred.metadata),
        )


# ────────────────────────────────────────────────────────────────────────────
# Process-local singleton
# ────────────────────────────────────────────────────────────────────────────


_singleton: Optional[CredentialsStore] = None


def get_store() -> CredentialsStore:
    global _singleton
    if _singleton is None:
        _singleton = CredentialsStore()
    return _singleton


def reset_store_for_tests() -> None:
    """Drop the module-level singleton. Tests call this between cases when
    they monkeypatch ``AGENTIC_CREDENTIALS_STATE_DIR`` so each test gets a
    fresh store rooted at its own tmp_path."""
    global _singleton
    _singleton = None
