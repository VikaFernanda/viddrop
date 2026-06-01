"""Secure credential storage for Viddrop.

Credentials are stored exclusively through the ``keyring`` library (backed by
libsecret/SecretStorage on Linux). They are NEVER written to SQLite or plain
files, and credential values are NEVER logged. Only the ``service_key`` and the
operation name are logged at DEBUG level.

Each credential pair is stored as two keyring entries:
- username under ``f"{service_key}:username"``
- password under ``f"{service_key}:password"``
"""

from __future__ import annotations

import keyring
import keyring.errors

from viddrop.utils.logger import log

# keyring service namespace shared by all Viddrop credential entries.
_KEYRING_SERVICE = "viddrop"


class CredentialStoreError(Exception):
    """Raised when the keyring backend is unavailable or fails."""


def _username_key(service_key: str) -> str:
    return f"{service_key}:username"


def _password_key(service_key: str) -> str:
    return f"{service_key}:password"


def store_credentials(service_key: str, username: str, password: str) -> None:
    """Store a username/password pair for ``service_key``.

    Raises:
        CredentialStoreError: if the keyring backend is unavailable.
    """
    log.debug("Credential store: storing for service_key=%s", service_key)
    try:
        keyring.set_password(_KEYRING_SERVICE, _username_key(service_key), username)
        keyring.set_password(_KEYRING_SERVICE, _password_key(service_key), password)
    except keyring.errors.KeyringError as exc:
        # Never include the exception detail at a level that could leak values;
        # the message references only the operation, not credential data.
        log.warning("Credential store: store failed for service_key=%s", service_key)
        raise CredentialStoreError(
            "Keyring backend unavailable while storing credentials"
        ) from exc


def get_credentials(service_key: str) -> dict[str, str] | None:
    """Return stored credentials for ``service_key`` or ``None`` if not found.

    Returns:
        ``{"username": ..., "password": ...}`` when both entries exist, or
        ``None`` when either is missing (not an error).

    Raises:
        CredentialStoreError: if the keyring backend is unavailable.
    """
    log.debug("Credential store: retrieving for service_key=%s", service_key)
    try:
        username = keyring.get_password(_KEYRING_SERVICE, _username_key(service_key))
        password = keyring.get_password(_KEYRING_SERVICE, _password_key(service_key))
    except keyring.errors.KeyringError as exc:
        log.warning(
            "Credential store: retrieve failed for service_key=%s", service_key
        )
        raise CredentialStoreError(
            "Keyring backend unavailable while retrieving credentials"
        ) from exc

    if username is None or password is None:
        return None
    return {"username": username, "password": password}


def delete_credentials(service_key: str) -> None:
    """Delete stored credentials for ``service_key``.

    A no-op if no credentials are stored. Raises ``CredentialStoreError`` only
    when the backend itself is unavailable.
    """
    log.debug("Credential store: deleting for service_key=%s", service_key)
    for key in (_username_key(service_key), _password_key(service_key)):
        try:
            keyring.delete_password(_KEYRING_SERVICE, key)
        except keyring.errors.PasswordDeleteError:
            # Entry not present: deletion is a no-op by contract.
            continue
        except keyring.errors.KeyringError as exc:
            log.warning(
                "Credential store: delete failed for service_key=%s", service_key
            )
            raise CredentialStoreError(
                "Keyring backend unavailable while deleting credentials"
            ) from exc
