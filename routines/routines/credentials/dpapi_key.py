"""Windows DPAPI-wrapped Fernet key for the credentials store.

Why two layers of encryption:
  * Fernet encrypts the credentials JSON file. Standard symmetric AES-128
    in CBC mode with an HMAC; the key is 32 bytes (urlsafe-base64-encoded).
  * The Fernet key itself is wrapped with **DPAPI**
    (``CryptProtectData``), tying decryptability to the operator's Windows
    user account. Copy the ``.dpapi`` file to a different user/machine and
    DPAPI refuses to unwrap. This is what closes the gap of "operator
    accidentally syncs ``state/`` via OneDrive."

Default storage: ``<routines-repo>/state/credentials.key.dpapi``. Override
via the ``AGENTIC_CREDENTIALS_STATE_DIR`` env var (tests use this).

The ``state/`` directory is already gitignored at the .gitignore root
rule; the explicit ``state/credentials.key.dpapi`` line is belt-and-braces.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)


DEFAULT_STATE_DIR_ENV = "AGENTIC_CREDENTIALS_STATE_DIR"
KEY_FILENAME = "credentials.key.dpapi"
_DESCRIPTION = "ANTON credentials Fernet key"


def _default_state_dir() -> Path:
    """Resolve where ``credentials.key.dpapi`` lives.

    Order:
      1. ``AGENTIC_CREDENTIALS_STATE_DIR`` env var (tests use this).
      2. ``<routines-repo>/state/``.
    """
    env = os.environ.get(DEFAULT_STATE_DIR_ENV)
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "state"


def _key_path() -> Path:
    d = _default_state_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / KEY_FILENAME


# ────────────────────────────────────────────────────────────────────────────
# DPAPI wrappers — Windows only. Tests can monkeypatch _wrap/_unwrap to run
# on non-Windows (the actual product is Windows-bound; the unit tests
# verify roundtrip, not platform coverage).
# ────────────────────────────────────────────────────────────────────────────


def _wrap(plaintext: bytes) -> bytes:
    """DPAPI-protect ``plaintext``. Raises ImportError on non-Windows."""
    import win32crypt  # type: ignore[import-not-found]
    return win32crypt.CryptProtectData(
        plaintext, _DESCRIPTION, None, None, None, 0,
    )


def _unwrap(wrapped: bytes) -> bytes:
    """DPAPI-unprotect ``wrapped``. Raises if the file was created under a
    different Windows user / machine."""
    import win32crypt  # type: ignore[import-not-found]
    _desc, plaintext = win32crypt.CryptUnprotectData(
        wrapped, None, None, None, 0,
    )
    return plaintext


# ────────────────────────────────────────────────────────────────────────────
# Public API
# ────────────────────────────────────────────────────────────────────────────


class FernetKeyError(RuntimeError):
    """Raised when the on-disk Fernet key cannot be unwrapped — typically
    because the file was moved across machines / users."""


def get_or_create_fernet_key() -> bytes:
    """Return the Fernet key, generating + DPAPI-wrapping one on first call.

    Side effect: creates ``<state-dir>/credentials.key.dpapi`` if absent.
    Subsequent calls read the existing file (no rotation; rotation is a
    deliberate operator action — delete the file + re-add credentials).
    """
    path = _key_path()
    if path.exists():
        wrapped = path.read_bytes()
        try:
            return _unwrap(wrapped)
        except Exception as e:  # noqa: BLE001 — DPAPI errors are pywin32-specific
            raise FernetKeyError(
                f"DPAPI unwrap failed for {path} — the file may have been "
                f"copied from a different Windows user / machine, or DPAPI "
                f"state is corrupted. Delete the file to regenerate (you'll "
                f"need to re-add every credential). Underlying: {e}"
            ) from e

    # First-run: generate a fresh Fernet key + DPAPI-wrap it.
    key = Fernet.generate_key()
    wrapped = _wrap(key)
    # Atomic write: write to .tmp, then rename. Avoids leaving a half-written
    # key file if the process is killed mid-write.
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(wrapped)
    tmp.replace(path)
    logger.info("credentials Fernet key generated + DPAPI-wrapped at %s", path)
    return key


def get_fernet() -> Fernet:
    """Convenience constructor."""
    return Fernet(get_or_create_fernet_key())


def reset_key_for_tests() -> None:
    """Delete the on-disk key file. Tests use this to force re-generation
    between cases without mutating the real ``state/`` dir (the env var
    monkeypatch routes both this and the store to ``tmp_path``)."""
    path = _key_path()
    if path.exists():
        path.unlink()
