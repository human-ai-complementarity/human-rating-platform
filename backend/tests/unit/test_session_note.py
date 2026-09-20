from __future__ import annotations

import pytest

from services.admin.rounds import build_session_note, format_session_length, with_session_note
from session_policy import SessionPolicy


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (5, "5 minutes"),
        (45, "45 minutes"),
        (60, "1 hour"),
        (90, "1 hour 30 minutes"),
        (120, "2 hours"),
        (110, "1 hour 50 minutes"),
        # The leftover minutes pluralise independently of the hours.
        (61, "1 hour 1 minute"),
        (121, "2 hours 1 minute"),
        (1, "1 minute"),
    ],
)
def test_reads_naturally_inside_a_sentence(minutes: int, expected: str) -> None:
    assert format_session_length(minutes) == expected


def test_note_states_the_limit_and_the_grace_window() -> None:
    note = build_session_note(SessionPolicy(duration_minutes=120, grace_minutes=5))

    assert "2 hours" in note
    assert "5 more minutes" in note


def test_note_omits_the_grace_sentence_when_there_is_no_grace() -> None:
    note = build_session_note(SessionPolicy(duration_minutes=60, grace_minutes=0))

    assert "more minute" not in note


def test_appends_after_the_researchers_own_words() -> None:
    """Their description is the substance; ours is a footnote to it."""
    combined = with_session_note("Read each passage.", SessionPolicy())

    assert combined.startswith("Read each passage.")
    assert combined.index("Time limit") > combined.index("Read each passage.")


def test_stands_alone_when_there_is_no_description() -> None:
    assert with_session_note("", SessionPolicy()).startswith("**Time limit:**")
    assert with_session_note("   \n ", SessionPolicy()).startswith("**Time limit:**")


def test_survives_the_prolific_html_converter() -> None:
    """The note has to render in Prolific's tag subset, not get stripped."""
    from services.prolific_markdown import to_prolific_html

    html = to_prolific_html(with_session_note("Read each passage.", SessionPolicy()))

    assert "<b>Time limit:</b>" in html
    assert "1 hour" in html
