"""Study-name template rendering (#96)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from services.admin.study_names import (
    NAME_MAX_LENGTH,
    ROUND_SUFFIX_RESERVE,
    TEMPLATE_MAX_LENGTH,
    check_card_templates,
    disambiguate,
    external_placeholders,
    render_study_name,
)

_CTX = {"dataset": "gpqa_diamond", "wave": "fall25", "method": "top_n"}


def _render(template: str, **kwargs) -> str:
    return render_study_name(template, context=_CTX, field="internal_study_name", **kwargs)


def test_whitelisted_placeholders_substitute():
    assert _render("{dataset} {wave} {method}") == "gpqa_diamond fall25 top_n"


def test_literal_template_passes_through():
    assert _render("Expert Q&A rating") == "Expert Q&A rating"


def test_unknown_placeholder_is_a_400_not_a_500():
    """A typo must not ship "{waves}" to Prolific as literal text."""
    with pytest.raises(HTTPException) as exc:
        _render("{waves}")
    assert exc.value.status_code == 400
    assert "{waves}" in exc.value.detail


def test_format_string_injection_is_rejected():
    with pytest.raises(HTTPException):
        _render("{0.__class__}")


@pytest.mark.parametrize("template", ["Study - Pilot", "Study - Round 2", "x -pilot"])
def test_templates_carrying_their_own_round_suffix_are_rejected(template):
    """rounds.py appends the round suffix, so this would double it."""
    with pytest.raises(HTTPException) as exc:
        _render(template)
    assert exc.value.status_code == 400


def test_render_leaves_room_for_the_round_suffix():
    """Nothing truncates before the name reaches Prolific.

    An overlong study name comes back as an opaque 502 from study creation, so
    the reservation has to happen here.
    """
    rendered = _render("x" * 400)
    assert len(rendered) == TEMPLATE_MAX_LENGTH
    assert len(rendered) + ROUND_SUFFIX_RESERVE == NAME_MAX_LENGTH
    assert len(f"{rendered} - Round 99") <= NAME_MAX_LENGTH


def test_external_name_excludes_the_arm():
    """A participant reads the study name before accepting, so naming the arm
    there risks biasing who self-selects in."""
    assert external_placeholders() == ("dataset",)
    with pytest.raises(HTTPException):
        render_study_name(
            "{dataset} {method}",
            context=_CTX,
            field="external_study_name",
            allowed=external_placeholders(),
        )


def test_card_templates_are_checked_by_the_render_rules():
    """Saving a card applies the rules rendering would, per field."""
    check_card_templates(
        {
            "external_study_name": "{dataset} rating",
            "internal_study_name": "{dataset} {wave} {method}",
            "study_blurb": "{anything} goes in prose",
        }
    )
    check_card_templates({"external_study_name": None})

    for values, field in (
        ({"external_study_name": "{dataset} {wave}"}, "external_study_name"),
        ({"internal_study_name": "{dataset} {waves}"}, "internal_study_name"),
        ({"internal_study_name": "{dataset} - Pilot"}, "internal_study_name"),
    ):
        with pytest.raises(HTTPException) as exc:
            check_card_templates(values)
        assert exc.value.status_code == 400
        assert field in exc.value.detail


def test_disambiguate_separates_identical_renders():
    """`experiments.name` has no unique constraint, so two arms of one group
    rendered from the same template would be indistinguishable in Prolific."""
    assert disambiguate("Study", set()) == "Study"
    assert disambiguate("Study", {"Study"}) == "Study (2)"
    assert disambiguate("Study", {"Study", "Study (2)"}) == "Study (3)"


def test_disambiguated_name_still_fits_the_column():
    long_name = "y" * TEMPLATE_MAX_LENGTH
    result = disambiguate(long_name, {long_name})
    assert len(result) <= TEMPLATE_MAX_LENGTH
    assert result.endswith(" (2)")
