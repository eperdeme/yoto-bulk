"""Tests for encrypted-at-rest library credentials.

The security property that matters: a Redis snapshot (dump.rdb) must never
contain a readable password.

Async helpers are driven with ``asyncio.run`` to match the convention in
``test_yoto_store.py``; the project does not depend on pytest-asyncio.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from borrowbox_server.credentials import (
    CredentialError,
    CredentialVault,
    derive_key,
    generate_secret_key,
    vault_for,
)

FERNET_KEY = generate_secret_key()
OTHER_KEY = generate_secret_key()

run = asyncio.run


class FakeStore:
    """Minimal in-memory stand-in for the Redis-backed Store."""

    def __init__(self) -> None:
        self.blobs: dict[str, str] = {}
        self.status: dict[str, dict] = {}

    async def set_credentials_blob(self, library: str, sealed: str) -> None:
        self.blobs[library] = sealed

    async def get_credentials_blob(self, library: str) -> str | None:
        return self.blobs.get(library)

    async def delete_credentials_blob(self, library: str) -> bool:
        return self.blobs.pop(library, None) is not None

    async def set_credential_status(self, library: str, status: dict) -> None:
        self.status[library] = status

    async def get_credential_status(self, library: str) -> dict | None:
        return self.status.get(library)

    async def delete_credential_status(self, library: str) -> None:
        self.status.pop(library, None)


class Settings:
    """Just the attributes vault_for() reads."""

    def __init__(self, secret_key: str = "", credentials: dict | None = None) -> None:
        self.secret_key = secret_key
        self.credentials = credentials or {}


def vault(secret=FERNET_KEY, env=None, store=None) -> CredentialVault:
    return CredentialVault(store if store is not None else FakeStore(), secret, env, quiet=True)


# ── key derivation ──────────────────────────────────────────────────────────


def test_generated_key_is_used_verbatim() -> None:
    assert derive_key(FERNET_KEY) == FERNET_KEY.encode()


def test_passphrase_is_stretched_not_used_raw() -> None:
    derived = derive_key("hunter2")
    assert derived != b"hunter2"
    assert len(derived) == 44  # urlsafe base64 of 32 bytes


def test_passphrase_derivation_is_deterministic() -> None:
    assert derive_key("hunter2") == derive_key("hunter2")


def test_different_passphrases_give_different_keys() -> None:
    assert derive_key("hunter2") != derive_key("hunter3")


def test_missing_secret_is_an_error() -> None:
    with pytest.raises(CredentialError):
        derive_key("")


# ── round trip ──────────────────────────────────────────────────────────────


def test_round_trip() -> None:
    v = vault()
    run(v.put("hounslow", "alice", "s3cret"))
    assert run(v.get("hounslow")) == ("alice", "s3cret")


def test_stored_blob_does_not_contain_the_password() -> None:
    """The whole point: a leaked dump.rdb must not reveal credentials."""
    store = FakeStore()
    run(vault(store=store).put("hounslow", "alice", "s3cret"))

    sealed = store.blobs["hounslow"]
    assert "s3cret" not in sealed
    assert "alice" not in sealed
    # Not merely encoded — the ciphertext must not parse as the JSON payload.
    with pytest.raises(json.JSONDecodeError):
        json.loads(sealed)


def test_wrong_key_cannot_decrypt_and_says_so() -> None:
    store = FakeStore()
    run(vault(store=store).put("hounslow", "alice", "s3cret"))

    with pytest.raises(CredentialError, match="secret key has changed"):
        run(vault(secret=OTHER_KEY, store=store).get("hounslow"))


def test_passphrase_secret_also_round_trips() -> None:
    v = vault(secret="a long passphrase")
    run(v.put("leeds", "bob", "pw"))
    assert run(v.get("leeds")) == ("bob", "pw")


# ── precedence ──────────────────────────────────────────────────────────────

ENV = {"devon": {"username": "env-user", "password": "env-pw"}}


def test_env_is_used_when_nothing_is_stored() -> None:
    assert run(vault(env=ENV).get("devon")) == ("env-user", "env-pw")


def test_store_overrides_env() -> None:
    v = vault(env=ENV)
    run(v.put("devon", "new-user", "new-pw"))
    assert run(v.get("devon")) == ("new-user", "new-pw")


def test_deleting_falls_back_to_env() -> None:
    v = vault(env=ENV)
    run(v.put("devon", "new-user", "new-pw"))
    run(v.delete("devon"))
    assert run(v.get("devon")) == ("env-user", "env-pw")


def test_unknown_library_returns_none() -> None:
    assert run(vault().get("nowhere")) is None


# ── no secret key configured ─────────────────────────────────────────────────


def test_without_a_secret_key_the_vault_is_read_only() -> None:
    v = vault(secret="")
    assert v.writable is False
    with pytest.raises(CredentialError, match="BORROWBOX_SECRET_KEY"):
        run(v.put("hounslow", "alice", "s3cret"))


def test_without_a_secret_key_env_credentials_still_work() -> None:
    """Existing deployments must not break by upgrading."""
    assert run(vault(secret="", env=ENV).get("devon")) == ("env-user", "env-pw")


def test_blank_credentials_are_rejected() -> None:
    with pytest.raises(CredentialError):
        run(vault().put("hounslow", "alice", ""))


# ── status reporting ────────────────────────────────────────────────────────


def test_status_reveals_username_but_never_password() -> None:
    v = vault()
    run(v.put("hounslow", "alice", "s3cret"))
    payload = run(v.status("hounslow")).to_dict()

    assert payload["has_credentials"] is True
    assert payload["source"] == "store"
    assert payload["username"] == "alice"
    assert "password" not in payload
    assert "s3cret" not in json.dumps(payload)


def test_status_distinguishes_store_from_env() -> None:
    v = vault(env=ENV)
    assert run(v.status("devon")).source == "env"
    run(v.put("devon", "u", "p"))
    assert run(v.status("devon")).source == "store"


def test_status_for_missing_credentials() -> None:
    status = run(vault().status("nowhere"))
    assert status.has_credentials is False
    assert status.source is None


def test_verification_outcome_is_recorded() -> None:
    v = vault()
    run(v.put("hounslow", "alice", "s3cret"))

    run(v.record_verification("hounslow", ok=False, error="401 rejected"))
    bad = run(v.status("hounslow"))
    assert bad.verified is False
    assert bad.error == "401 rejected"
    assert bad.verified_at is not None

    run(v.record_verification("hounslow", ok=True))
    good = run(v.status("hounslow"))
    assert good.verified is True
    assert good.error is None


def test_undecryptable_record_is_reported_not_hidden() -> None:
    """A changed secret key must surface in the listing, not silently vanish."""
    store = FakeStore()
    run(vault(store=store).put("hounslow", "alice", "s3cret"))

    status = run(vault(secret=OTHER_KEY, store=store).status("hounslow"))
    assert status.has_credentials is True
    assert status.verified is False
    assert "decrypt" in (status.error or "")


# ── helper ──────────────────────────────────────────────────────────────────


def test_vault_for_reads_settings() -> None:
    v = vault_for(FakeStore(), Settings(FERNET_KEY, ENV))
    assert v.writable is True
    assert run(v.get("devon")) == ("env-user", "env-pw")


def test_vault_for_tolerates_a_missing_store() -> None:
    """Some call paths pass store=None; env credentials must still resolve."""
    assert run(vault_for(None, Settings(FERNET_KEY, ENV)).get("devon")) == ("env-user", "env-pw")
