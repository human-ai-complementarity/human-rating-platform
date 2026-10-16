"""Rater terms: manifest validation and placeholder rendering."""

from __future__ import annotations

import json

import pytest

from services.terms import (
    CONSENT_STANDARD,
    CONSENT_WARNED,
    DEBRIEF,
    TermsSourceError,
    find_placeholders,
    format_session_length,
    parse_manifest,
    render_markdown,
    statement_path,
)


def _manifest(documents: dict) -> str:
    return json.dumps({"documents": documents})


def test_parse_manifest_reads_the_three_document_versions() -> None:
    manifest = parse_manifest(_manifest({CONSENT_STANDARD: 2, CONSENT_WARNED: 1, DEBRIEF: 3}))
    assert manifest.version_of(CONSENT_STANDARD) == 2
    assert manifest.version_of(CONSENT_WARNED) == 1
    assert manifest.version_of(DEBRIEF) == 3
    assert statement_path(CONSENT_WARNED, 1) == "consent/warned/v1.md"
    assert statement_path(DEBRIEF, 3) == "debrief/v3.md"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not json", "not valid JSON"),
        ("[]", "must be a JSON object"),
        (json.dumps({}), "needs a 'documents' object"),
        (_manifest({CONSENT_STANDARD: 1, CONSENT_WARNED: 1}), "missing \\['debrief'\\]"),
        (
            _manifest({CONSENT_STANDARD: 1, CONSENT_WARNED: 1, DEBRIEF: 1, "consent/x": 1}),
            "unknown \\['consent/x'\\]",
        ),
        (_manifest({CONSENT_STANDARD: 0, CONSENT_WARNED: 1, DEBRIEF: 1}), "positive integer"),
        (_manifest({CONSENT_STANDARD: "1", CONSENT_WARNED: 1, DEBRIEF: 1}), "positive integer"),
        (_manifest({CONSENT_STANDARD: True, CONSENT_WARNED: 1, DEBRIEF: 1}), "positive integer"),
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
