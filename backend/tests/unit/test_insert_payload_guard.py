from __future__ import annotations

import pytest

from sqlalchemy import event

from config import UploadSettings, get_settings
from database import (
    _PAYLOAD_GUARD_HEADROOM,
    _guard_question_insert_payload,
    _payload_size,
    build_database,
)

CEILING = UploadSettings().max_insert_payload_bytes * _PAYLOAD_GUARD_HEADROOM


def _guard(statement: str, parameters: object) -> None:
    """Invoke the guard with the positional shape SQLAlchemy passes it."""
    _guard_question_insert_payload(None, None, statement, parameters, None, False)


class TestPayloadSize:
    """Parameter shapes differ by driver and by executemany vs. one statement."""

    def test_flat_sequence(self):
        assert _payload_size(["ab", "cde"]) == 5

    def test_sequence_of_rows(self):
        assert _payload_size([["ab", "c"], ["de", "f"]]) == 6

    def test_sequence_of_dicts(self):
        assert _payload_size([{"a": "xx"}, {"a": "yyy"}]) == 5

    def test_single_dict(self):
        assert _payload_size({"a": "xx", "b": "y"}) == 3

    def test_ignores_non_string_values(self):
        assert _payload_size([1, None, True, "ab"]) == 2

    def test_empty(self):
        assert _payload_size([]) == 0
        assert _payload_size(None) == 0


class TestGuard:
    def test_ignores_statements_against_other_tables(self):
        # Well over the ceiling, but not the table whose rows carry documents.
        _guard("INSERT INTO ratings (value) VALUES (%s)", ["x" * (CEILING + 1)])

    def test_ignores_leading_whitespace_when_matching(self):
        with pytest.raises(RuntimeError):
            _guard(
                "\n  INSERT INTO questions (question_text) VALUES (%s)",
                ["x" * (CEILING + 1)],
            )

    def test_allows_a_batched_payload(self):
        _guard("INSERT INTO questions (question_text) VALUES (%s)", ["x" * (CEILING // 2)])

    def test_allows_one_deliberately_oversized_row(self):
        """The batcher emits a row bigger than its own cap alone, by design.

        The ceiling has to clear that case or correct behaviour would raise.
        """
        oversized = "x" * (get_settings().uploads.max_insert_payload_bytes + 1)
        _guard("INSERT INTO questions (question_text) VALUES (%s)", [oversized])

    def test_raises_on_an_unbatched_payload(self):
        with pytest.raises(RuntimeError, match="Refusing an unbatched INSERT into questions"):
            _guard(
                "INSERT INTO questions (question_text) VALUES (%s)",
                [["x" * 1024] for _ in range(CEILING // 1024 + 1)],
            )

    def test_error_names_the_helper_to_use(self):
        """The message has to tell the next author what to do instead."""
        with pytest.raises(RuntimeError, match="insert_questions_in_batches"):
            _guard(
                "INSERT INTO questions (question_text) VALUES (%s)",
                ["x" * (CEILING + 1)],
            )


@pytest.mark.asyncio
async def test_guard_is_attached_to_the_application_engine():
    """The guard is worthless if it is correct but never wired up.

    A guard that silently listens to nothing is worse than none at all, since it
    reads as coverage. This asserts the listener is actually on the engine the
    app runs its sessions through.
    """
    database = build_database()
    await database.connect()
    try:
        assert event.contains(
            database._engine.sync_engine,
            "before_cursor_execute",
            _guard_question_insert_payload,
        )
    finally:
        await database.disconnect()
