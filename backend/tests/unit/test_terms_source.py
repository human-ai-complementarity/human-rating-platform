"""The terms source reader: how each URL scheme maps to a read."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from config import TermsSettings
from services.terms import CONSENT_STANDARD, TermsSourceError, read_statement
from services.terms import source as terms_source


def test_file_source_reads_an_absolute_folder(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    settings = TermsSettings(source_url=f"file://{tmp_path}")
    assert asyncio.run(terms_source.fetch_text(settings, "manifest.json")) == "{}"

    with pytest.raises(TermsSourceError, match="Could not read"):
        asyncio.run(terms_source.fetch_text(settings, "missing.md"))


def test_file_source_resolves_a_relative_folder_against_the_backend_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "terms").mkdir()
    (tmp_path / "terms" / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(terms_source, "BASE_DIR", tmp_path)
    # Surrounding whitespace in the env var must not shift the path.
    settings = TermsSettings(source_url=" file://terms ")
    assert asyncio.run(terms_source.fetch_text(settings, "manifest.json")) == "{}"
    assert terms_source.source_url_for(settings, "manifest.json") == "file://terms/manifest.json"


def test_file_source_treats_non_utf8_as_unreadable(tmp_path: Path) -> None:
    (tmp_path / "bad.md").write_bytes(b"\xff\xfe not text")
    settings = TermsSettings(source_url=f"file://{tmp_path}")
    with pytest.raises(TermsSourceError, match="Could not read"):
        asyncio.run(terms_source.fetch_text(settings, "bad.md"))


def test_https_source_fetches_under_the_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith("/missing.md"):
            return httpx.Response(404)
        if request.url.path.endswith("/binary.md"):
            return httpx.Response(200, content=b"\xff\xfe")
        return httpx.Response(200, text="## body")

    monkeypatch.setattr(terms_source, "_http_transport", httpx.MockTransport(handler))
    settings = TermsSettings(source_url="https://terms.example.org/rater-terms/")

    assert asyncio.run(terms_source.fetch_text(settings, "consent/standard/v1.md")) == "## body"
    assert seen == ["https://terms.example.org/rater-terms/consent/standard/v1.md"]
    with pytest.raises(TermsSourceError, match="HTTP 404"):
        asyncio.run(terms_source.fetch_text(settings, "missing.md"))
    with pytest.raises(TermsSourceError, match="not UTF-8"):
        asyncio.run(terms_source.fetch_text(settings, "binary.md"))


def test_https_transport_failures_surface_as_source_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    monkeypatch.setattr(terms_source, "_http_transport", httpx.MockTransport(handler))
    with pytest.raises(TermsSourceError, match="Could not fetch"):
        asyncio.run(
            terms_source.fetch_text(TermsSettings(source_url="https://x.test"), "manifest.json")
        )


def test_malformed_https_url_is_a_source_error() -> None:
    # httpx.InvalidURL is not an HTTPError; without naming it this would be a 500.
    with pytest.raises(TermsSourceError, match="Could not fetch"):
        asyncio.run(
            terms_source.fetch_text(TermsSettings(source_url="https://[::1/terms"), "manifest.json")
        )


def test_plain_http_is_refused() -> None:
    with pytest.raises(TermsSourceError, match="must use https://"):
        asyncio.run(
            terms_source.fetch_text(TermsSettings(source_url="http://x.test"), "manifest.json")
        )


def test_gs_source_reads_bucket_and_prefixed_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_download(settings: TermsSettings, bucket_name: str, blob_name: str) -> str:
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
    def failing_download(settings: TermsSettings, bucket_name: str, blob_name: str) -> str:
        raise TermsSourceError(f"gs://{bucket_name}/{blob_name} not found")

    monkeypatch.setattr(terms_source, "_download_gcs_text", failing_download)
    with pytest.raises(TermsSourceError, match="not found"):
        asyncio.run(terms_source.fetch_text(TermsSettings(source_url="gs://b/p"), "manifest.json"))

    with pytest.raises(TermsSourceError, match="needs a bucket name"):
        asyncio.run(terms_source.fetch_text(TermsSettings(source_url="gs://"), "x"))


def test_gs_download_maps_every_failure_to_a_source_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real download path, with the client stubbed: each failure class the
    read can raise becomes a TermsSourceError rather than escaping as a 500."""
    from google.api_core import exceptions as gcs_exceptions
    from google.auth import exceptions as auth_exceptions

    class _Blob:
        def __init__(self, error: Exception | None) -> None:
            self.error = error

        def download_as_text(self, encoding: str) -> str:
            if self.error:
                raise self.error
            return "## body"

    class _Bucket:
        def __init__(self, error: Exception | None) -> None:
            self.error = error

        def blob(self, name: str) -> _Blob:
            return _Blob(self.error)

    class _Client:
        def __init__(self, error: Exception | None) -> None:
            self.error = error

        def bucket(self, name: str) -> _Bucket:
            return _Bucket(self.error)

    settings = TermsSettings(source_url="gs://b/p")
    monkeypatch.setattr(terms_source, "_gcs_client", lambda *key: _Client(None))
    assert terms_source._download_gcs_text(settings, "b", "p/manifest.json") == "## body"

    for error, message in [
        (gcs_exceptions.NotFound("gone"), "not found"),
        (gcs_exceptions.Forbidden("no"), "No permission"),
        (gcs_exceptions.ServiceUnavailable("503"), "Could not read"),
        (auth_exceptions.TransportError("tls"), "Could not read"),
        (ConnectionResetError("reset"), "Could not read"),
        (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"), "Could not read"),
    ]:
        monkeypatch.setattr(terms_source, "_gcs_client", lambda *key, e=error: _Client(e))
        with pytest.raises(TermsSourceError, match=message):
            terms_source._download_gcs_text(settings, "b", "p/manifest.json")


def test_gs_client_identity_comes_from_the_settings_passed_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keys: list[tuple[str, str, str]] = []

    class _Blob:
        def download_as_text(self, encoding: str) -> str:
            return "x"

    class _Bucket:
        def blob(self, name: str) -> _Blob:
            return _Blob()

    class _Client:
        def bucket(self, name: str) -> _Bucket:
            return _Bucket()

    def fake_client(*key: str) -> _Client:
        keys.append(key)
        return _Client()

    monkeypatch.setattr(terms_source, "_gcs_client", fake_client)
    settings = TermsSettings(
        source_url="gs://b",
        gcs_wif_audience="//iam.googleapis.com/aud",
        gcs_wif_token_file="/token",
        gcs_impersonate_service_account="sa@example.iam.gserviceaccount.com",
    )
    terms_source._download_gcs_text(settings, "b", "manifest.json")
    assert keys == [("//iam.googleapis.com/aud", "/token", "sa@example.iam.gserviceaccount.com")]


def test_unknown_scheme_is_refused() -> None:
    with pytest.raises(TermsSourceError, match="must start with gs://"):
        asyncio.run(terms_source.fetch_text(TermsSettings(source_url="ftp://x"), "manifest.json"))


def test_read_statement_validates_content(tmp_path: Path) -> None:
    folder = tmp_path / "consent" / "standard"
    folder.mkdir(parents=True)
    settings = TermsSettings(source_url=f"file://{tmp_path}")

    (folder / "v1.md").write_text("   \n", encoding="utf-8")
    with pytest.raises(TermsSourceError, match="is empty"):
        asyncio.run(read_statement(settings, CONSENT_STANDARD, 1))

    (folder / "v2.md").write_text("Hello {{study_name}} and {{whoops}}", encoding="utf-8")
    with pytest.raises(TermsSourceError, match="unknown placeholder"):
        asyncio.run(read_statement(settings, CONSENT_STANDARD, 2))

    (folder / "v3.md").write_text("Hello {{study_name}}", encoding="utf-8")
    assert asyncio.run(read_statement(settings, CONSENT_STANDARD, 3)) == "Hello {{study_name}}"


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
