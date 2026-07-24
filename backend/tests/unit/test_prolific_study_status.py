from __future__ import annotations

import pytest

from models import ProlificStudyStatus


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("AWAITING_REVIEW", ProlificStudyStatus.AWAITING_REVIEW),
        # Prolific's API returns this status space-separated.
        ("AWAITING REVIEW", ProlificStudyStatus.AWAITING_REVIEW),
        ("awaiting review", ProlificStudyStatus.AWAITING_REVIEW),
        ("  AWAITING REVIEW  ", ProlificStudyStatus.AWAITING_REVIEW),
        ("ACTIVE", ProlificStudyStatus.ACTIVE),
    ],
)
def test_coerces_space_and_underscore_variants(raw: str, expected: ProlificStudyStatus) -> None:
    assert ProlificStudyStatus(raw) is expected


def test_coerced_value_serializes_with_underscore() -> None:
    # Downstream (DB, API responses, frontend) expects the underscore form.
    assert ProlificStudyStatus("AWAITING REVIEW").value == "AWAITING_REVIEW"


def test_unknown_status_still_raises() -> None:
    with pytest.raises(ValueError):
        ProlificStudyStatus("TOTALLY_UNKNOWN")
