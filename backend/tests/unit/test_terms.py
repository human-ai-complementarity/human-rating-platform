"""Rater terms: manifest validation and placeholder rendering."""

from __future__ import annotations

import json

import pytest

from services.terms import (
    TermsSourceError,
    find_placeholders,
    format_session_length,
    parse_manifest,
    render_markdown,
)


def _manifest(bundles: dict) -> str:
    return json.dumps({"schema": 1, "bundles": bundles})


def test_parse_manifest_reads_bundles_and_versions() -> None:
    manifest = parse_manifest(
        _manifest(
            {
                "standard": {"label": "Standard", "content_warnings": ["none"], "consent": 2},
                "sensitive": {
                    "label": "Sensitive",
                    "content_warnings": ["sensitive", "explicit"],
                    "consent": 1,
                    "debrief": 3,
                },
            }
        )
    )
    standard = manifest.bundle("standard")
    assert standard is not None
    assert standard.consent_version == 2
    assert standard.debrief_version is None
    assert standard.permits("none") and not standard.permits("sensitive")
    sensitive = manifest.bundle("sensitive")
    assert sensitive is not None
    assert sensitive.debrief_version == 3
    assert sensitive.permits("explicit")
    assert manifest.bundle("missing") is None


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not json", "not valid JSON"),
        ("[]", "must be a JSON object"),
        (json.dumps({"schema": 1}), "non-empty 'bundles'"),
        (_manifest({"Bad Key": {"content_warnings": ["none"], "consent": 1}}), "invalid"),
        (_manifest({"a": {"content_warnings": [], "consent": 1}}), "content_warnings"),
        (_manifest({"a": {"content_warnings": ["scary"], "consent": 1}}), "content_warnings"),
        (_manifest({"a": {"content_warnings": ["none"], "consent": 0}}), "positive integer"),
        (_manifest({"a": {"content_warnings": ["none"], "consent": "1"}}), "positive integer"),
        (
            _manifest({"a": {"content_warnings": ["sensitive"], "consent": 1}}),
            "needs a 'debrief' version",
        ),
        (_manifest({"a": {"content_warnings": ["none"], "consent": 1, "label": ""}}), "label"),
    ],
)
def test_parse_manifest_rejects_bad_input(raw: str, message: str) -> None:
    with pytest.raises(TermsSourceError, match=message):
        parse_manifest(raw)


def test_find_placeholders_and_render() -> None:
    text = "Study {{study_name}} lasts {{ session_length }}. {{content_warning_details}}"
    assert find_placeholders(text) == {"study_name", "session_length", "content_warning_details"}
    rendered = render_markdown(
        text, study_name="Pilot", session_length="1 hour", content_warning_details=None
    )
    assert rendered == "Study Pilot lasts 1 hour. "
    assert "{{" not in rendered


def test_render_leaves_unknown_placeholders_alone() -> None:
    # read_statement refuses these before they get here; rendering must not
    # silently drop them either.
    assert (
        render_markdown(
            "{{nope}}", study_name="x", session_length="y", content_warning_details=None
        )
        == "{{nope}}"
    )


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (1, "1 minute"),
        (45, "45 minutes"),
        (60, "1 hour"),
        (90, "1 hour 30 minutes"),
        (120, "2 hours"),
    ],
)
def test_format_session_length(minutes: int, expected: str) -> None:
    assert format_session_length(minutes) == expected
