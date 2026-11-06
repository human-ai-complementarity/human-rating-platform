from __future__ import annotations

import pytest
from pydantic import ValidationError

from config import AppSettings, Settings


def test_cors_origins_model_default_is_wildcard() -> None:
    assert AppSettings().cors_origins == ["*"]


def test_cors_origins_uses_toml_value_when_env_not_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP__CORS_ORIGINS", raising=False)
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret")

    settings = Settings(_env_file=None)

    assert settings.app.cors_origins == [
        "http://localhost:5173",
        "http://localhost:8000",
    ]


def test_cors_origins_accepts_json_array_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "APP__CORS_ORIGINS",
        '["https://app.example.com","http://localhost:5173"]',
    )
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret")

    settings = Settings(_env_file=None)

    assert settings.app.cors_origins == [
        "https://app.example.com",
        "http://localhost:5173",
    ]


def test_cors_origins_rejects_csv_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "APP__CORS_ORIGINS",
        "http://localhost:5173,http://localhost:8000",
    )
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret")

    with pytest.raises(ValidationError, match="APP__CORS_ORIGINS must be a JSON array of strings"):
        Settings(_env_file=None)


def test_cors_origins_rejects_invalid_json_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP__CORS_ORIGINS", '["https://app.example.com",]')
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret")

    with pytest.raises(ValidationError, match="APP__CORS_ORIGINS must be a JSON array of strings"):
        Settings(_env_file=None)


def test_media_source_url_must_be_gs_or_file() -> None:
    from config import MediaSettings

    assert MediaSettings(source_url=" gs://bucket/media ").source_url == "gs://bucket/media"
    assert MediaSettings(source_url="file://media").source_url == "file://media"
    assert MediaSettings(source_url="").source_url == ""
    with pytest.raises(ValidationError, match="must be gs://bucket/prefix or file://folder"):
        MediaSettings(source_url="https://example.com/media")
    with pytest.raises(ValidationError, match="needs a bucket name"):
        MediaSettings(source_url="gs:///media")
