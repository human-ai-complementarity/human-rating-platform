from __future__ import annotations

from services.question_separator import (
    separator_upload_offenders,
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


def test_offenders_skip_clean_rows():
    rows = [
        {"question_id": "q1", "question_text": "Plain question?"},
        {
            "question_id": "q2",
            "question_text": "Doc\n\n--- QUESTION ---\nWhat follows?",
        },
        {"question_id": "q3", "question_text": ""},
    ]
    assert separator_upload_offenders(rows) == ["'q2'"]


def test_offenders_skip_parent_rows_referenced_in_the_same_upload():
    rows = [
        {
            "question_id": "parent1",
            "question_text": "Keep me intact\n\n--- QUESTION ---\nstill the document",
            "parent_question_id": "",
        },
        {
            "question_id": "child1",
            "question_text": "What follows?",
            "parent_question_id": "parent1",
        },
    ]
    assert separator_upload_offenders(rows) == []


def test_offenders_still_flag_a_child_that_uses_the_delimiter():
    rows = [
        {"question_id": "parent1", "question_text": "Short context", "parent_question_id": ""},
        {
            "question_id": "child1",
            "question_text": "Doc\n\n--- QUESTION ---\nWhat follows?",
            "parent_question_id": "parent1",
        },
    ]
    assert separator_upload_offenders(rows) == ["'child1'"]


def test_offenders_label_blank_question_id_by_row_number():
    rows = [
        {"question_id": "", "question_text": "Doc\n\n--- QUESTION ---\nWhat follows?"},
        {"question_id": "q2", "question_text": "Fine"},
    ]
    assert separator_upload_offenders(rows) == ["row 1"]


def test_offenders_collapse_duplicate_question_ids():
    rows = [
        {"question_id": "q1", "question_text": "Doc\n\n--- QUESTION ---\nFirst?"},
        {"question_id": "q1", "question_text": "Doc\n\n--- QUESTION ---\nSecond?"},
    ]
    assert separator_upload_offenders(rows) == ["'q1'"]
