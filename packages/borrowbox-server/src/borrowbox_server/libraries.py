"""The library registry: which BorrowBox libraries exist.

Libraries used to come only from ``BORROWBOX_LIBRARIES`` as env JSON, so adding
one meant editing ``.env`` and restarting. They now live in Redis and are
managed from the UI.

Design note
-----------
Around twenty call sites read ``settings.libraries``. Rather than thread a
registry object through all of them, the authoritative dict from Redis is
copied *into* that same dict object in place, so every existing reader keeps
working and immediately sees changes.

The environment is treated as a one-time seed: on first boot the registry is
populated from ``BORROWBOX_LIBRARIES``, a marker is set, and from then on the
store is authoritative. That way removing a library through the UI is not undone
by the next restart.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# A library name is used in URL paths and Redis keys; a subdomain is interpolated
# into https://{subdomain}.borrowbox.com. Both are validated strictly rather than
# trusted, so neither can smuggle in a path segment or a different host.
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,48}$")
_SUBDOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class LibraryError(ValueError):
    """Raised when a library name or subdomain is not acceptable."""


def normalise_name(value: str) -> str:
    """Validate and canonicalise a library name."""
    name = (value or "").strip().lower()
    if not _NAME_RE.match(name):
        raise LibraryError(
            "Library name must be lowercase letters, digits, dashes or underscores "
            "(1-49 characters)."
        )
    return name


def normalise_subdomain(value: str) -> str:
    """Validate and canonicalise a BorrowBox subdomain.

    Rejects anything that is not a bare DNS label, so a value like
    ``evil.example.com/`` cannot redirect requests off-site.
    """
    sub = (value or "").strip().lower()
    # Accept a pasted full host by taking its first label.
    if "." in sub:
        sub = sub.split(".", 1)[0]
    if not _SUBDOMAIN_RE.match(sub):
        raise LibraryError(
            "Subdomain must be a single DNS label: lowercase letters, digits or dashes."
        )
    return sub


async def refresh(store, settings) -> dict[str, str]:
    """Reload the registry from Redis into ``settings.libraries``.

    Seeds from the environment the first time only. Returns the resulting
    mapping.
    """
    stored = await store.get_libraries()

    if not await store.libraries_initialised():
        seed = dict(getattr(settings, "libraries", None) or {})
        if seed:
            for name, subdomain in seed.items():
                try:
                    await store.set_library(normalise_name(name), normalise_subdomain(subdomain))
                except LibraryError:
                    logger.warning(
                        "Skipping invalid seeded library %r -> %r", name, subdomain
                    )
            logger.info(
                "Seeded library registry from BORROWBOX_LIBRARIES: %s", sorted(seed)
            )
        else:
            # Mark as initialised even when empty, so an operator who adds
            # libraries purely through the UI is never overridden by a stale
            # environment variable later.
            await store.mark_libraries_initialised()
        stored = await store.get_libraries()

    _apply(settings, stored)
    return stored


def _apply(settings, mapping: dict[str, str]) -> None:
    """Replace the contents of ``settings.libraries`` in place.

    In place matters: existing readers hold a reference to this dict.
    """
    current = getattr(settings, "libraries", None)
    if isinstance(current, dict):
        current.clear()
        current.update(mapping)
    else:  # pragma: no cover - defensive
        settings.libraries = dict(mapping)


async def add(store, settings, name: str, subdomain: str) -> dict[str, str]:
    """Register a library and make it live immediately."""
    await store.set_library(normalise_name(name), normalise_subdomain(subdomain))
    return await refresh(store, settings)


async def remove(store, settings, name: str) -> bool:
    """Deregister a library. Returns whether it existed."""
    existed = await store.delete_library(normalise_name(name))
    # Keep the marker so the environment cannot resurrect it on restart.
    await store.mark_libraries_initialised()
    await refresh(store, settings)
    return existed
