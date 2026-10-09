"""Tests for the Redis-backed library registry.

Two properties matter:

* the environment is a one-time seed, so a library removed in the UI stays
  removed across restarts;
* names and subdomains are validated, because the subdomain is interpolated into
  ``https://{subdomain}.borrowbox.com``.
"""

from __future__ import annotations

import asyncio

import pytest
from borrowbox_server import libraries as registry
from borrowbox_server.libraries import LibraryError, normalise_name, normalise_subdomain

run = asyncio.run


class FakeStore:
    """In-memory stand-in for the registry parts of Store."""

    def __init__(self, libraries: dict[str, str] | None = None, initialised: bool = False) -> None:
        self.libraries = dict(libraries or {})
        self.initialised = initialised

    async def get_libraries(self) -> dict[str, str]:
        return dict(self.libraries)

    async def set_library(self, name: str, subdomain: str) -> None:
        self.libraries[name] = subdomain
        self.initialised = True

    async def delete_library(self, name: str) -> bool:
        return self.libraries.pop(name, None) is not None

    async def libraries_initialised(self) -> bool:
        return self.initialised

    async def mark_libraries_initialised(self) -> None:
        self.initialised = True


class Settings:
    def __init__(self, libraries: dict[str, str] | None = None) -> None:
        self.libraries = dict(libraries or {})


# ── validation ──────────────────────────────────────────────────────────────


def test_name_is_lowercased_and_trimmed() -> None:
    assert normalise_name("  Hounslow  ") == "hounslow"


@pytest.mark.parametrize(
    "bad", ["", "  ", "-leading", "has space", "has/slash", "x" * 50, "UPPER!"]
)
def test_invalid_names_rejected(bad: str) -> None:
    with pytest.raises(LibraryError):
        normalise_name(bad)


def test_subdomain_accepts_a_pasted_host() -> None:
    """Operators paste the URL they sign in at; take the first label."""
    assert normalise_subdomain("hounslow.borrowbox.com") == "hounslow"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "  ",
        "-leading",
        "has space",
        "has_underscore",  # not valid in a DNS label
        "/evil",
        "..",
    ],
)
def test_invalid_subdomains_rejected(bad: str) -> None:
    with pytest.raises(LibraryError):
        normalise_subdomain(bad)


def test_subdomain_cannot_smuggle_a_different_host() -> None:
    """A value like 'evil.example.com/' must not survive as a full host."""
    assert normalise_subdomain("evil.example.com") == "evil"
    with pytest.raises(LibraryError):
        normalise_subdomain("/evil.example.com")


# ── seeding ─────────────────────────────────────────────────────────────────


def test_env_seeds_the_registry_on_first_boot() -> None:
    store = FakeStore()
    settings = Settings({"hounslow": "hounslow", "leeds": "leeds"})
    result = run(registry.refresh(store, settings))
    assert result == {"hounslow": "hounslow", "leeds": "leeds"}
    assert store.initialised is True


def test_seeding_happens_only_once() -> None:
    """After the first boot the store is authoritative, not the environment."""
    store = FakeStore({"hounslow": "hounslow"}, initialised=True)
    settings = Settings({"hounslow": "hounslow", "leeds": "leeds"})  # env still lists two
    assert run(registry.refresh(store, settings)) == {"hounslow": "hounslow"}


def test_removed_library_is_not_resurrected_by_env() -> None:
    """The regression this design exists to prevent."""
    store = FakeStore()
    settings = Settings({"hounslow": "hounslow", "leeds": "leeds"})
    run(registry.refresh(store, settings))          # first boot seeds both
    run(registry.remove(store, settings, "leeds"))  # operator removes one
    run(registry.refresh(store, settings))          # restart
    assert "leeds" not in settings.libraries


def test_empty_env_still_marks_initialised() -> None:
    """A UI-only deployment must not be overridden by a later env var."""
    store = FakeStore()
    settings = Settings({})
    run(registry.refresh(store, settings))
    assert store.initialised is True


def test_invalid_seed_entries_are_skipped_not_fatal() -> None:
    store = FakeStore()
    settings = Settings({"good": "good", "bad name": "nope!"})
    result = run(registry.refresh(store, settings))
    assert "good" in result
    assert "bad name" not in result


# ── mutation ────────────────────────────────────────────────────────────────


def test_add_updates_settings_in_place() -> None:
    """Twenty call sites hold a reference to this dict; it must be mutated."""
    store = FakeStore(initialised=True)
    settings = Settings()
    original = settings.libraries
    run(registry.add(store, settings, "devon", "devon"))
    assert settings.libraries is original
    assert original["devon"] == "devon"


def test_add_normalises_input() -> None:
    store = FakeStore(initialised=True)
    settings = Settings()
    run(registry.add(store, settings, "  Devon ", "devon.borrowbox.com"))
    assert settings.libraries == {"devon": "devon"}


def test_add_rejects_bad_input() -> None:
    store = FakeStore(initialised=True)
    with pytest.raises(LibraryError):
        run(registry.add(store, Settings(), "ok", "not a subdomain"))


def test_remove_reports_whether_it_existed() -> None:
    store = FakeStore({"devon": "devon"}, initialised=True)
    settings = Settings({"devon": "devon"})
    assert run(registry.remove(store, settings, "devon")) is True
    assert run(registry.remove(store, settings, "devon")) is False
    assert settings.libraries == {}
