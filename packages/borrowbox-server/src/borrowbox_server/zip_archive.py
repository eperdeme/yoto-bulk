"""Persistent ZIP archive for BorrowBox downloads.

Downloaded ZIPs are kept permanently so that import retries never need to
re-download from BorrowBox (which rate-limits aggressively) and as a
long-term backup of the raw audiobook files.

Files are stored as ``{archive_dir}/{library}/{title} [{loan_id}].zip``
so the archive is human-browsable.  Lookups search by loan_id suffix so
they work even if the title was unknown at first save.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def _sanitize(name: str) -> str:
    """Make a string safe for use as a filename component."""
    # Replace path separators and other problematic chars
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    # Collapse whitespace
    name = re.sub(r"\s+", " ", name).strip()
    # Truncate to keep paths reasonable
    return name[:120] if name else "Unknown"


def _zip_path(archive_dir: str, library: str, loan_id: str, title: str | None = None) -> Path:
    label = _sanitize(title) if title else loan_id
    return Path(archive_dir) / library / f"{label} [{loan_id}].zip"


def _find_by_loan_id(archive_dir: str, library: str, loan_id: str) -> Path | None:
    """Find an archived ZIP by loan_id, regardless of the title in the filename.

    Also checks the legacy ``{loan_id}.zip`` naming from the initial
    implementation.
    """
    lib_dir = Path(archive_dir) / library
    if not lib_dir.is_dir():
        return None
    # New format: "{title} [{loan_id}].zip"
    suffix = f"[{loan_id}].zip"
    for path in lib_dir.iterdir():
        if path.name.endswith(suffix):
            return path
    # Legacy format: "{loan_id}.zip"
    legacy = lib_dir / f"{loan_id}.zip"
    if legacy.is_file():
        return legacy
    return None


def list_archives(archive_dir: str) -> list[dict[str, str]]:
    """List all archived ZIPs across all libraries.

    Returns a list of dicts with ``library``, ``loan_id``, ``title``,
    ``filename``, and ``size_mb``.
    """
    root = Path(archive_dir)
    if not root.is_dir():
        return []

    results: list[dict[str, str]] = []
    bracket_re = re.compile(r"\[(\d+)\]\.zip$")
    for lib_dir in sorted(root.iterdir()):
        if not lib_dir.is_dir():
            continue
        library = lib_dir.name
        for path in sorted(lib_dir.iterdir()):
            if not path.suffix == ".zip":
                continue
            name = path.name
            m = bracket_re.search(name)
            if m:
                loan_id = m.group(1)
                title = name[: m.start()].strip()
            else:
                # Legacy: {loan_id}.zip
                loan_id = path.stem
                title = ""
            size_bytes = path.stat().st_size
            results.append({
                "library": library,
                "loan_id": loan_id,
                "title": title,
                "filename": name,
                "size_mb": f"{size_bytes / (1024 * 1024):.1f}",
            })
    return results


def get_archived_zip(archive_dir: str, library: str, loan_id: str) -> bytes | None:
    """Return archived ZIP bytes, or *None* if not yet archived."""
    path = _find_by_loan_id(archive_dir, library, loan_id)
    if path and path.is_file():
        logger.info("Using archived ZIP for %s/%s (%s)", library, loan_id, path.name)
        return path.read_bytes()
    return None


def save_zip(
    archive_dir: str, library: str, loan_id: str, data: bytes, *, title: str | None = None,
) -> Path:
    """Write ZIP bytes to the archive.  Creates directories as needed.

    If a file for this loan_id already exists (possibly with a different
    title), it is renamed to match the current title.
    """
    desired = _zip_path(archive_dir, library, loan_id, title)
    existing = _find_by_loan_id(archive_dir, library, loan_id)

    if existing and existing != desired and title:
        # Rename to include the now-known title
        existing.rename(desired)
        logger.info("Renamed archived ZIP %s → %s", existing.name, desired.name)
        return desired

    desired.parent.mkdir(parents=True, exist_ok=True)
    desired.write_bytes(data)
    logger.info(
        "Archived ZIP for %s/%s (%d bytes) → %s", library, loan_id, len(data), desired.name
    )
    return desired
