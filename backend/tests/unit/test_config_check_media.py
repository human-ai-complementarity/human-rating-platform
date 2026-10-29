"""The predeploy check on MEDIA__* mirrors what the media reader accepts."""

from __future__ import annotations

import pytest

from config import MediaSettings, get_settings
from scripts.config_check import RenderValidator, ValidationResult


def _check(**media: object) -> ValidationResult:
    settings = get_settings().model_copy(update={"media": MediaSettings(**media)}, deep=True)
    result = ValidationResult()
    RenderValidator().check_media_source(settings, result)
    return result


def test_unset_source_is_fine_since_only_image_datasets_need_one() -> None:
    result = _check(source_url="")
    assert result.ok and not result.warnings


@pytest.mark.parametrize(
    "media",
    [
        {"source_url": "gs://bucket/media"},
        {"source_url": "gs://bucket/media", "signed_urls": False, "gcs_wif_audience": "//iam"},
        {
            "source_url": "gs://bucket/media",
            "gcs_wif_audience": "//iam",
            "gcs_impersonate_service_account": "hrp-media@example.iam.gserviceaccount.com",
        },
    ],
)
def test_usable_sources_pass(media: dict) -> None:
    result = _check(**media)
    assert result.ok and not result.warnings


def test_file_source_warns() -> None:
    assert "file://" in _check(source_url="file://media").warnings[0]


def test_unsupported_scheme_fails_predeploy() -> None:
    result = _check(source_url="https://example.com/media")
    assert not result.ok
    assert "unsupported scheme" in result.errors[0]


def test_federated_signing_without_a_signer_fails_predeploy() -> None:
    result = _check(source_url="gs://bucket/media", gcs_wif_audience="//iam")
    assert not result.ok
    assert "MEDIA__GCS_IMPERSONATE_SERVICE_ACCOUNT" in result.errors[0]
