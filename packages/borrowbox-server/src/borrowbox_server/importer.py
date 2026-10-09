"""BorrowBox import staging helpers.

This isolates the temporary LocalLibrary-based parsing path so routes do not
accidentally treat it as live library state.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yoto_bulk.library import LocalLibrary


@dataclass
class BorrowBoxImportStage:
    temp_root: Path
    library: LocalLibrary
    result: dict[str, Any]

    @property
    def card_id(self) -> str:
        return str(self.result["card_id"])

    def load_payload(self) -> dict[str, Any] | None:
        return self.library.load_card(self.card_id)

    def save_payload(self, payload: dict[str, Any]) -> None:
        self.library.save_card(self.card_id, payload)

    def audio_files(self) -> list[Path]:
        return self.library.audio_files_for_card(self.card_id)

    def cover_file(self) -> Path | None:
        return self.library.cover_file_for_card(self.card_id)

    def cleanup(self) -> None:
        shutil.rmtree(self.temp_root, ignore_errors=True)


def stage_borrowbox_zip_import(zip_bytes: bytes, *, title: str | None = None) -> BorrowBoxImportStage:
    """Import a BorrowBox zip into a temporary staging workspace."""
    temp_root = Path(tempfile.mkdtemp(prefix="borrowbox-import-"))
    library = LocalLibrary(temp_root / "library")
    library.ensure_directories()

    tmp_path = temp_root / "loan.zip"
    tmp_path.write_bytes(zip_bytes)
    try:
        result = library.import_from_zip(tmp_path, title=title, include_id3_description=False)
    finally:
        tmp_path.unlink(missing_ok=True)

    return BorrowBoxImportStage(temp_root=temp_root, library=library, result=result)
