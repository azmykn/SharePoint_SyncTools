"""Local symmetric secret encryption for sensitive SQLite keys (cookies & OAuth tokens).

Uses ``cryptography.fernet.Fernet`` with a per-database key stored alongside the SQLite
file (``.vault.key`` with ``0600`` permissions on POSIX). Plaintext legacy rows are read
transparently so existing databases migrate seamlessly without losing sessions.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

_logger = logging.getLogger(__name__)

_ENC_PREFIX = "enc:v1:"

try:
    from cryptography.fernet import Fernet, InvalidToken

    _FERNET_AVAILABLE = True
except Exception:
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment]
    _FERNET_AVAILABLE = False


def _chmod_private(path: str) -> None:
    if os.name == "posix":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _key_file_for_db(db_path: str) -> str:
    parent = os.path.dirname(os.path.abspath(db_path)) or "."
    return os.path.join(parent, ".vault.key")


def _load_or_create_fernet(db_path: str) -> Optional["Fernet"]:
    if not _FERNET_AVAILABLE:
        return None
    env_key = os.environ.get("SP_SYNC_VAULT_KEY", "").strip()
    if env_key:
        try:
            return Fernet(env_key.encode("ascii"))
        except Exception as exc:
            _logger.warning("Invalid SP_SYNC_VAULT_KEY; falling back to key file: %s", exc)

    key_path = _key_file_for_db(db_path)
    try:
        if os.path.isfile(key_path):
            with open(key_path, "rb") as f:
                key = f.read().strip()
            if key:
                _chmod_private(key_path)
                return Fernet(key)
        os.makedirs(os.path.dirname(key_path) or ".", exist_ok=True)
        key = Fernet.generate_key()
        tmp = key_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(key)
        _chmod_private(tmp)
        os.replace(tmp, key_path)
        _chmod_private(key_path)
        return Fernet(key)
    except Exception as exc:
        _logger.warning("Local vault key unavailable (%s); storing secret unencrypted", exc)
        return None


def encrypt_secret(plaintext: str, db_path: str) -> str:
    """Encrypt *plaintext* with local Fernet key. Returns original text if crypto unavailable."""
    if not plaintext:
        return ""
    if plaintext.startswith(_ENC_PREFIX):
        return plaintext
    fernet = _load_or_create_fernet(db_path)
    if fernet is None:
        return plaintext
    try:
        token = fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        return _ENC_PREFIX + token
    except Exception as exc:
        _logger.warning("Failed to encrypt secret: %s", exc)
        return plaintext


def decrypt_secret(stored: str, db_path: str) -> str:
    """Decrypt ``enc:v1:...`` secret, or return legacy plaintext unchanged."""
    if not stored:
        return ""
    if not stored.startswith(_ENC_PREFIX):
        return stored
    fernet = _load_or_create_fernet(db_path)
    if fernet is None:
        _logger.error("Encrypted secret found in DB but cryptography is unavailable")
        return ""
    token = stored[len(_ENC_PREFIX) :].encode("ascii")
    try:
        return fernet.decrypt(token).decode("utf-8")
    except InvalidToken:
        _logger.warning("Could not decrypt stored secret (key mismatch or corrupted token)")
        return ""
    except Exception as exc:
        _logger.warning("Error decrypting stored secret: %s", exc)
        return ""


def is_encrypted(stored: str) -> bool:
    return isinstance(stored, str) and stored.startswith(_ENC_PREFIX)
