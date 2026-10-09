from __future__ import annotations

from yoto_bulk.card_utils import resolve_description, sanitize_description, truncate_for_api


def test_sanitize_description_strips_html_and_decodes_entities() -> None:
    raw = "<p>It's &amp; almost time</p><p>Line&nbsp;two<br>Line three</p>"

    assert sanitize_description(raw) == "It's & almost time\n\nLine two\nLine three"


def test_resolve_description_returns_sanitized_text() -> None:
    card = {"metadata": {"description": "<p>Hello <strong>world</strong></p>"}}

    assert resolve_description(card) == "Hello world"


def test_truncate_for_api_sanitizes_description_in_place() -> None:
    payload = {"metadata": {"description": "<p>Hello&nbsp;world</p>"}}

    truncate_for_api(payload)

    assert payload["metadata"]["description"] == "Hello world"
