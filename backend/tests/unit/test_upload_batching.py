from __future__ import annotations

from models import Question
from config import UploadSettings
from services.admin.question_inserts import (
    _batch_by_payload_size,
    _question_payload_size,
)

# The shipped default, so these cases exercise realistic batch boundaries and
# stay in step if the default is ever retuned.
MAX_INSERT_PAYLOAD_BYTES = UploadSettings().max_insert_payload_bytes


def _question(question_id: str, text_bytes: int) -> Question:
    return Question(
        experiment_id=1,
        question_id=question_id,
        question_text="x" * text_bytes,
        gt_answer="A",
        options="A,B,C,D",
        question_type="MC",
        extra_data="{}",
    )


def _batch_bytes(batch: list[Question]) -> int:
    return sum(_question_payload_size(q) for q in batch)


def test_short_rows_pack_into_a_single_batch():
    questions = [_question(f"q{i}", 100) for i in range(50)]

    batches = list(_batch_by_payload_size(questions, MAX_INSERT_PAYLOAD_BYTES))

    assert len(batches) == 1
    assert len(batches[0]) == 50


def test_long_context_rows_split_into_bounded_batches():
    # 40 rows of 500KB each: one statement would carry ~20MB of parameters,
    # which is what OOM-killed a small Postgres instance in production.
    questions = [_question(f"q{i}", 500 * 1024) for i in range(40)]

    batches = list(_batch_by_payload_size(questions, MAX_INSERT_PAYLOAD_BYTES))

    assert len(batches) > 1
    for batch in batches:
        assert _batch_bytes(batch) <= MAX_INSERT_PAYLOAD_BYTES


def test_every_row_is_emitted_exactly_once_and_in_order():
    questions = [_question(f"q{i}", 300 * 1024) for i in range(25)]

    batched = [
        q for batch in _batch_by_payload_size(questions, MAX_INSERT_PAYLOAD_BYTES) for q in batch
    ]

    assert [q.question_id for q in batched] == [f"q{i}" for i in range(25)]


def test_a_single_oversized_row_gets_its_own_batch_rather_than_being_dropped():
    questions = [
        _question("small-before", 10),
        _question("huge", MAX_INSERT_PAYLOAD_BYTES * 2),
        _question("small-after", 10),
    ]

    batches = list(_batch_by_payload_size(questions, MAX_INSERT_PAYLOAD_BYTES))

    assert [q.question_id for batch in batches for q in batch] == [
        "small-before",
        "huge",
        "small-after",
    ]
    assert ["huge"] in [[q.question_id for q in batch] for batch in batches]


def test_payload_size_counts_multibyte_text_in_bytes_not_characters():
    question = _question("q1", 0)
    question.question_text = "é" * 100  # 2 bytes each in UTF-8

    # 200 bytes of text, plus the short fixed fields.
    assert _question_payload_size(question) > 200


def test_empty_input_yields_no_batches():
    assert list(_batch_by_payload_size([], MAX_INSERT_PAYLOAD_BYTES)) == []
