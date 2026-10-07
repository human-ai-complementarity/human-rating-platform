"""Per-round Prolific study naming.

The card's templates differentiate *experiments*; these suffixes differentiate
*rounds* within one. Covered directly because #96's launch gate means the
blank-internal-name branch is no longer reachable through the API — it stays
for rows created before the gate existed.
"""

from __future__ import annotations

import pytest

from services.admin.rounds import _build_round_internal_name, _build_round_study_name


@pytest.mark.parametrize(
    "round_number,expected",
    [(0, "Study - Pilot"), (1, "Study - Round 1"), (12, "Study - Round 12")],
)
def test_study_name_carries_the_round(round_number, expected):
    assert _build_round_study_name("Study", round_number) == expected


def test_internal_name_carries_the_round():
    assert _build_round_internal_name("Internal", 0) == "Internal - Pilot"
    assert _build_round_internal_name("Internal", 3) == "Internal - Round 3"


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_internal_name_is_omitted_when_blank(blank):
    """Returns None so the field is left off the Prolific payload entirely."""
    assert _build_round_internal_name(blank, 0) is None
