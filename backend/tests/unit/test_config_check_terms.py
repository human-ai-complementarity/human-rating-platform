"""The predeploy check on TERMS__SOURCE_URL mirrors what the reader accepts."""

from __future__ import annotations

import pytest

from config import TermsSettings, get_settings
from scripts.config_check import RenderValidator, ValidationResult


def _check(source_url: str) -> ValidationResult:
    settings = get_settings().model_copy(
        update={"terms": TermsSettings(source_url=source_url)}, deep=True
    )
    result = ValidationResult()
    RenderValidator().check_terms_source_set(settings, result)
    return result


@pytest.mark.parametrize("source_url", ["gs://bucket/rater-terms", "https://terms.example.org/x"])
def test_usable_sources_pass(source_url: str) -> None:
    result = _check(source_url)
    assert result.ok and not result.warnings


def test_unset_and_file_sources_warn() -> None:
    assert "placeholder" in _check("").warnings[0]
    assert "file://" in _check("file://terms").warnings[0]


@pytest.mark.parametrize(
    ("source_url", "message"),
    [("http://terms.example.org/x", "http://"), ("ftp://terms", "unsupported scheme")],
)
def test_sources_the_reader_refuses_fail_predeploy(source_url: str, message: str) -> None:
    result = _check(source_url)
    assert not result.ok
    assert message in result.errors[0]
