from __future__ import annotations

from services.question_separator import (
    question_ids_with_separator,
    split_separator_question,
)


def test_split_returns_none_without_delimiter():
    assert split_separator_question("Just a question?") is None


def test_split_returns_none_when_only_one_newline_before_marker():
    assert split_separator_question("Document\n--- QUESTION ---\nQuestion?") is None


def test_split_returns_none_when_either_side_is_empty():
    assert split_separator_question("\n\n--- QUESTION ---\nQuestion?") is None
    assert split_separator_question("Document\n\n--- QUESTION ---\n") is None


def test_split_uses_last_match_and_trims():
    text = (
        "First block\n\n--- QUESTION ---\nMiddle still document\n\n--- QUESTION ---\n"
        "  The actual question?  "
    )
    assert split_separator_question(text) == (
        "First block\n\n--- QUESTION ---\nMiddle still document",
        "The actual question?",
    )


def test_split_accepts_crlf():
    text = "Document line\r\n\r\n--- QUESTION ---\r\nWhich answer?"
    assert split_separator_question(text) == ("Document line", "Which answer?")


def test_question_ids_with_separator_skips_clean_rows():
    rows = [
        {"question_id": "q1", "question_text": "Plain question?"},
        {
            "question_id": "q2",
            "question_text": "Doc\n\n--- QUESTION ---\nWhat follows?",
        },
        {"question_id": "q3", "question_text": ""},
    ]
    assert question_ids_with_separator(rows) == ["q2"]
