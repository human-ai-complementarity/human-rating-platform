"""Unit tests for the participant-group exclusion filter builder.

These are pure-function tests; they don't touch Prolific or the DB. The
integration-side coverage (round create/update, add-on-start_session) lives
in tests/e2e/test_characterization.py.
"""

from __future__ import annotations

from services.admin.prolific import build_exclusion_filters, build_screener_filters


def test_build_exclusion_filters_empty_returns_empty_list():
    assert build_exclusion_filters(None) == []
    assert build_exclusion_filters([]) == []


def test_build_exclusion_filters_wraps_group_ids():
    result = build_exclusion_filters(["group_a", "group_b"])
    assert result == [
        {
            "filter_id": "participant_group_blocklist",
            "selected_values": ["group_a", "group_b"],
        }
    ]


def test_build_exclusion_filters_drops_falsy_ids():
    # Callers may pass a list with None/"" placeholders when an excluded
    # experiment has no group yet. Those get filtered out, and if nothing is
    # left the filter is omitted entirely (so we don't send a filter with an
    # empty selected_values, which Prolific would reject).
    assert build_exclusion_filters([None, ""]) == []
    assert build_exclusion_filters(["", "real_id", None]) == [
        {"filter_id": "participant_group_blocklist", "selected_values": ["real_id"]}
    ]


def test_screener_and_exclusion_filters_compose_via_concat():
    # The round-launch and round-update code paths combine the two filter
    # lists with `+`. Assert the resulting order matches the natural
    # "screeners first, then exclusion" reading.
    combined = build_screener_filters(["ai_taskers"]) + build_exclusion_filters(["g1"])
    assert combined == [
        {"filter_id": "ai-taskers", "selected_values": ["0"]},
        {"filter_id": "participant_group_blocklist", "selected_values": ["g1"]},
    ]
