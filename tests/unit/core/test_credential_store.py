"""Unit tests for the credential_store module.

All keyring access is mocked: these tests never touch the real OS keyring.
"""

from __future__ import annotations

import keyring.errors
import pytest

from viddrop.core import credential_store
from viddrop.core.credential_store import CredentialStoreError


class FakeKeyring:
    """In-memory stand-in for the keyring module's password store."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, name: str, value: str) -> None:
        self.store[(service, name)] = value

    def get_password(self, service: str, name: str) -> str | None:
        return self.store.get((service, name))

    def delete_password(self, service: str, name: str) -> None:
        key = (service, name)
        if key not in self.store:
            raise keyring.errors.PasswordDeleteError("not found")
        del self.store[key]


@pytest.fixture
def fake_keyring(monkeypatch) -> FakeKeyring:
    fake = FakeKeyring()
    monkeypatch.setattr(credential_store.keyring, "set_password", fake.set_password)
    monkeypatch.setattr(credential_store.keyring, "get_password", fake.get_password)
    monkeypatch.setattr(
        credential_store.keyring, "delete_password", fake.delete_password
    )
    return fake


# ----------------------------------------------------------------------- #
# Happy path
# ----------------------------------------------------------------------- #


def test_store_then_get_returns_dict(fake_keyring):
    credential_store.store_credentials("example.com", "alice", "s3cret")
    creds = credential_store.get_credentials("example.com")
    assert creds == {"username": "alice", "password": "s3cret"}


def test_delete_then_get_returns_none(fake_keyring):
    credential_store.store_credentials("example.com", "alice", "s3cret")
    credential_store.delete_credentials("example.com")
    assert credential_store.get_credentials("example.com") is None


# ----------------------------------------------------------------------- #
# Failure path
# ----------------------------------------------------------------------- #


def test_get_raises_credential_store_error_on_keyring_error(monkeypatch):
    def boom(service: str, name: str) -> str | None:
        raise keyring.errors.KeyringError("backend down")

    monkeypatch.setattr(credential_store.keyring, "get_password", boom)
    with pytest.raises(CredentialStoreError):
        credential_store.get_credentials("example.com")


def test_store_raises_credential_store_error_on_keyring_error(monkeypatch):
    def boom(service: str, name: str, value: str) -> None:
        raise keyring.errors.KeyringError("backend down")

    monkeypatch.setattr(credential_store.keyring, "set_password", boom)
    with pytest.raises(CredentialStoreError):
        credential_store.store_credentials("example.com", "alice", "s3cret")


# ----------------------------------------------------------------------- #
# Edge cases
# ----------------------------------------------------------------------- #


def test_get_nonexistent_returns_none(fake_keyring):
    assert credential_store.get_credentials("nonexistent") is None


def test_get_returns_none_when_only_username_present(fake_keyring):
    # Username stored but password missing -> incomplete -> None.
    fake_keyring.set_password("viddrop", "example.com:username", "alice")
    assert credential_store.get_credentials("example.com") is None


def test_delete_nonexistent_is_noop(fake_keyring):
    # Should not raise even though nothing is stored.
    credential_store.delete_credentials("nonexistent")


def test_delete_raises_on_non_delete_keyring_error(monkeypatch):
    def boom(service: str, name: str) -> None:
        raise keyring.errors.KeyringError("backend down")

    monkeypatch.setattr(credential_store.keyring, "delete_password", boom)
    with pytest.raises(CredentialStoreError):
        credential_store.delete_credentials("example.com")


# ----------------------------------------------------------------------- #
# Security
# ----------------------------------------------------------------------- #


def test_password_never_logged(fake_keyring, monkeypatch):
    logged: list[str] = []

    def capture(msg: str, *args, **kwargs) -> None:
        # Render the format string the way logging would.
        logged.append(msg % args if args else msg)

    monkeypatch.setattr(credential_store.log, "debug", capture)
    monkeypatch.setattr(credential_store.log, "info", capture)
    monkeypatch.setattr(credential_store.log, "warning", capture)

    secret = "super-secret-value"
    credential_store.store_credentials("example.com", "alice", secret)
    credential_store.get_credentials("example.com")
    credential_store.delete_credentials("example.com")

    assert logged, "expected at least one log line"
    for line in logged:
        assert secret not in line
        assert "alice" not in line
