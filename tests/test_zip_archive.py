"""Tests for borrowbox_server.zip_archive."""

from pathlib import Path

from borrowbox_server.zip_archive import get_archived_zip, save_zip


def test_roundtrip(tmp_path: Path) -> None:
    data = b"PK\x03\x04fake-zip-content"
    result = save_zip(str(tmp_path), "leeds", "12345", data, title="Isadora Moon")

    assert result == tmp_path / "leeds" / "Isadora Moon [12345].zip"
    assert result.read_bytes() == data

    loaded = get_archived_zip(str(tmp_path), "leeds", "12345")
    assert loaded == data


def test_missing_returns_none(tmp_path: Path) -> None:
    assert get_archived_zip(str(tmp_path), "leeds", "99999") is None


def test_separate_libraries(tmp_path: Path) -> None:
    save_zip(str(tmp_path), "leeds", "111", b"aaa", title="Book A")
    save_zip(str(tmp_path), "hounslow", "222", b"bbb", title="Book B")

    assert get_archived_zip(str(tmp_path), "leeds", "111") == b"aaa"
    assert get_archived_zip(str(tmp_path), "hounslow", "222") == b"bbb"
    assert get_archived_zip(str(tmp_path), "leeds", "222") is None


def test_no_title_uses_loan_id(tmp_path: Path) -> None:
    result = save_zip(str(tmp_path), "leeds", "999", b"data")
    assert result.name == "999 [999].zip"
    assert get_archived_zip(str(tmp_path), "leeds", "999") == b"data"


def test_rename_on_title_update(tmp_path: Path) -> None:
    """First save without title, second save with title renames the file."""
    save_zip(str(tmp_path), "leeds", "555", b"content")
    old = tmp_path / "leeds" / "555 [555].zip"
    assert old.exists()

    save_zip(str(tmp_path), "leeds", "555", b"content", title="The Gruffalo")
    assert not old.exists()
    renamed = tmp_path / "leeds" / "The Gruffalo [555].zip"
    assert renamed.exists()
    # Content preserved (rename, not rewrite)
    assert renamed.read_bytes() == b"content"
