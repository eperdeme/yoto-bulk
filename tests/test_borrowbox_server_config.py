from __future__ import annotations

from borrowbox_server.config import Settings
from yoto_server.config import Settings as YotoSettings


def test_borrowbox_server_abs_settings_fall_back_to_shared_yoto_bulk_env(monkeypatch) -> None:
    monkeypatch.delenv("BORROWBOX_ABS_URL", raising=False)
    monkeypatch.delenv("BORROWBOX_ABS_TOKEN", raising=False)
    monkeypatch.delenv("BORROWBOX_ABS_CF_ACCESS_CLIENT_ID", raising=False)
    monkeypatch.delenv("BORROWBOX_ABS_CF_ACCESS_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("BORROWBOX_ABS_LIBRARY_NAME", raising=False)
    monkeypatch.setenv("YOTO_BULK_ABS_URL", "https://shared-abs.example")
    monkeypatch.setenv("YOTO_BULK_ABS_TOKEN", "shared-token")
    monkeypatch.setenv("YOTO_BULK_ABS_CF_ACCESS_CLIENT_ID", "shared-access-client-id")
    monkeypatch.setenv("YOTO_BULK_ABS_CF_ACCESS_CLIENT_SECRET", "shared-access-client-secret")
    monkeypatch.setenv("YOTO_BULK_ABS_LIBRARY_NAME", "Books")

    settings = Settings(_env_file=None)

    assert settings.abs_url == "https://shared-abs.example"
    assert settings.abs_token == "shared-token"
    assert settings.abs_cf_access_client_id == "shared-access-client-id"
    assert settings.abs_cf_access_client_secret == "shared-access-client-secret"
    assert settings.abs_library_name == "Books"


def test_yoto_server_loads_abs_cloudflare_access_credentials(monkeypatch) -> None:
    monkeypatch.setenv("YOTO_BULK_ABS_CF_ACCESS_CLIENT_ID", "access-client-id")
    monkeypatch.setenv("YOTO_BULK_ABS_CF_ACCESS_CLIENT_SECRET", "access-client-secret")

    settings = YotoSettings(_env_file=None)

    assert settings.abs_cf_access_client_id == "access-client-id"
    assert settings.abs_cf_access_client_secret == "access-client-secret"
