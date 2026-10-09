"""The terms source reader: how each URL scheme maps to a read."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from config import TermsSettings
from services.terms import TermsSourceError
from services.terms import source as terms_source


def test_file_source_reads_relative_to_backend_dir(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    settings = TermsSettings(source_url=f"file://{tmp_path}")
    assert asyncio.run(terms_source.fetch_text(settings, "manifest.json")) == "{}"

    with pytest.raises(TermsSourceError, match="Could not read"):
        asyncio.run(terms_source.fetch_text(settings, "missing.md"))


def test_gs_source_reads_bucket_and_prefixed_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_download(bucket_name: str, blob_name: str) -> str:
        calls.append((bucket_name, blob_name))
        return "## body"

    monkeypatch.setattr(terms_source, "_download_gcs_text", fake_download)

    settings = TermsSettings(source_url="gs://complementarities/rater-terms/")
    text = asyncio.run(terms_source.fetch_text(settings, "consent/standard/v1.md"))
    assert text == "## body"
    assert calls == [("complementarities", "rater-terms/consent/standard/v1.md")]

    # No prefix: objects sit at the bucket root.
    asyncio.run(terms_source.fetch_text(TermsSettings(source_url="gs://bucket"), "manifest.json"))
    assert calls[-1] == ("bucket", "manifest.json")


def test_gs_source_errors_surface_as_source_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_download(bucket_name: str, blob_name: str) -> str:
        raise TermsSourceError(f"gs://{bucket_name}/{blob_name} not found")

    monkeypatch.setattr(terms_source, "_download_gcs_text", failing_download)
    with pytest.raises(TermsSourceError, match="not found"):
        asyncio.run(terms_source.fetch_text(TermsSettings(source_url="gs://b/p"), "manifest.json"))

    with pytest.raises(TermsSourceError, match="needs a bucket name"):
        asyncio.run(terms_source.fetch_text(TermsSettings(source_url="gs://"), "x"))


def test_unknown_scheme_is_refused() -> None:
    with pytest.raises(TermsSourceError, match="must start with gs://"):
        asyncio.run(terms_source.fetch_text(TermsSettings(source_url="ftp://x"), "manifest.json"))


def test_wif_credentials_are_built_from_settings_without_any_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = tmp_path / "token"
    token.write_text("not-a-real-jwt", encoding="utf-8")
    settings = TermsSettings(
        source_url="gs://complementarities/rater-terms",
        gcs_wif_audience=(
            "//iam.googleapis.com/projects/1/locations/global/"
            "workloadIdentityPools/render-pool/providers/render-provider"
        ),
        gcs_impersonate_service_account="hrp-rater-terms@example.iam.gserviceaccount.com",
    )

    # Explicit file wins; otherwise Render's AWS_WEB_IDENTITY_TOKEN_FILE is used.
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", str(token))
    assert terms_source.wif_token_file(settings) == str(token)
    explicit = settings.model_copy(update={"gcs_wif_token_file": "/elsewhere"})
    assert terms_source.wif_token_file(explicit) == "/elsewhere"

    credentials = terms_source.wif_credentials(settings, str(token))
    info = credentials.info
    assert info["type"] == "external_account"
    assert info["audience"] == settings.gcs_wif_audience
    assert info["subject_token_type"] == "urn:ietf:params:oauth:token-type:jwt"
    assert info["token_url"] == "https://sts.googleapis.com/v1/token"
    assert info["credential_source"] == {"file": str(token)}
    assert info["service_account_impersonation_url"].endswith(
        "hrp-rater-terms@example.iam.gserviceaccount.com:generateAccessToken"
    )
    # The token file is read lazily at exchange time, never at construction.
    assert credentials.token is None


def test_unset_source_is_refused_with_a_pointer() -> None:
    # The platform ships no statements: an unset source must fail loudly, not
    # fall back to anything.
    with pytest.raises(TermsSourceError, match="TERMS__SOURCE_URL is not set"):
        asyncio.run(terms_source.fetch_text(TermsSettings(source_url=""), "manifest.json"))
