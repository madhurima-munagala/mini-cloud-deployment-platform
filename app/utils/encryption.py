"""
Environment-variable encryption utility.

Implements the security approach from DATABASE_DESIGN.md §5 and
API_CONTRACT.md §6/§9.0:

- environment_variables.value_encrypted is the ONLY place a variable's
  value is ever stored — there is no plaintext column.
- Encryption/decryption is isolated here so the (not-yet-built) env var
  API and deployment/engine-trigger service can both depend on this one
  module rather than re-implementing crypto logic.
- The Fernet key comes from Settings (environment variable), never
  hardcoded, never logged.
- Nothing in this module ever logs or prints a plaintext value. Callers
  must follow the same rule — see the warnings on decrypt_value() below.

This module deliberately does NOT implement the environment-variable API
endpoints (GET/PUT /repositories/{id}/env) — that is out of scope for the
current database-layer task.
"""

import re

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

# Heuristic used to set environment_variables.is_sensitive at write time,
# matching the masking rule described in API_CONTRACT.md §6.1.
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(KEY|SECRET|TOKEN|PASSWORD)", re.IGNORECASE
)

_MASK = "\u2022" * 8  # "••••••••" — same mask shown in the API contract examples


class EncryptionError(Exception):
    """Raised when a value cannot be encrypted or decrypted."""


def _get_fernet() -> Fernet:
    """
    Builds a Fernet instance from the configured key on every call rather
    than caching it at import time, so tests can swap ENCRYPTION_KEY
    (via a fresh Settings instance) without import-order surprises.
    """
    try:
        return Fernet(settings.encryption_key.encode("utf-8"))
    except (ValueError, TypeError) as exc:
        raise EncryptionError(
            "ENCRYPTION_KEY is not a valid Fernet key. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        ) from exc


def is_sensitive_key(key: str) -> bool:
    """
    Returns True if an environment variable's key name suggests it holds
    a secret (contains KEY, SECRET, TOKEN, or PASSWORD, case-insensitive).

    This is computed once at write time and stored in
    environment_variables.is_sensitive, so reads never need to re-run
    this heuristic or touch the encrypted value just to decide whether to
    mask it.
    """
    return bool(_SENSITIVE_KEY_PATTERN.search(key))


def encrypt_value(plaintext_value: str) -> str:
    """
    Encrypts a plaintext environment variable value for storage in
    environment_variables.value_encrypted.

    Returns a string safe to store in a TEXT column (Fernet tokens are
    URL-safe base64).
    """
    if plaintext_value is None:
        raise EncryptionError("Cannot encrypt a None value.")
    fernet = _get_fernet()
    token = fernet.encrypt(plaintext_value.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_value(encrypted_value: str) -> str:
    """
    Decrypts a value_encrypted string back to plaintext.

    WARNING: the caller is responsible for never logging, printing, or
    otherwise exposing the return value. Per API_CONTRACT.md, the only
    legitimate use of this function in the wider system is building the
    request body sent to the deployment engine (§9.1) over the internal,
    network-restricted channel. It must never be used to populate a
    frontend-facing API response.
    """
    fernet = _get_fernet()
    try:
        plaintext = fernet.decrypt(encrypted_value.encode("utf-8"))
    except InvalidToken as exc:
        raise EncryptionError(
            "Could not decrypt value — wrong key, or the stored value is corrupted."
        ) from exc
    return plaintext.decode("utf-8")


def mask_value(is_sensitive: bool, decrypted_value: str | None = None) -> str:
    """
    Returns what the API should show for a stored env var value.

    - Sensitive keys always return the fixed mask, regardless of value.
    - Non-sensitive keys return the actual value as-is (still requires the
      caller to have decrypted it deliberately — this function does not
      decrypt anything itself).

    Kept here (not in the API layer, which doesn't exist yet) so the
    masking rule lives next to the encryption rule it depends on.
    """
    if is_sensitive:
        return _MASK
    return decrypted_value if decrypted_value is not None else _MASK
