"""The flag table: what each study flag implies, in one place."""

from __future__ import annotations

import pytest

from services.terms import (
    CONSENT_STANDARD,
    CONSENT_WARNED,
    DEBRIEF,
    FLAG_EFFECTS,
    StudyFlag,
    parse_flags,
    requirements_for,
)


def test_every_flag_has_an_entry() -> None:
    assert set(FLAG_EFFECTS) == set(StudyFlag)


def test_no_flags_is_an_ordinary_study() -> None:
    needs = requirements_for([])
    assert needs.flags == ()
    assert needs.content_warning is None
    assert needs.prescreeners == ()
    assert needs.consent_document == CONSENT_STANDARD
    assert needs.debrief_document is None
    assert not needs.warned


@pytest.mark.parametrize("flag", ["sensitive", "explicit"])
def test_content_flags_warn_prescreen_and_use_the_warned_consent(flag: str) -> None:
    needs = requirements_for([flag])
    assert needs.content_warning == flag
    assert needs.prescreeners == ("harmful_content",)
    assert needs.consent_document == CONSENT_WARNED
    assert needs.debrief_document is None
    assert needs.warned


def test_deception_adds_its_prescreener_and_the_debrief_only() -> None:
    needs = requirements_for(["deception"])
    assert needs.content_warning is None
    assert needs.prescreeners == ("deception",)
    assert needs.consent_document == CONSENT_STANDARD
    assert needs.debrief_document == DEBRIEF


def test_flags_combine_and_the_stronger_warning_wins() -> None:
    needs = requirements_for(["deception", "sensitive", "explicit", "sensitive"])
    assert needs.flags == (StudyFlag.SENSITIVE, StudyFlag.EXPLICIT, StudyFlag.DECEPTION)
    # Prolific takes one content warning per study: the stronger one is sent.
    assert needs.content_warning == "explicit"
    assert needs.prescreeners == ("harmful_content", "deception")
    assert needs.consent_document == CONSENT_WARNED
    assert needs.debrief_document == DEBRIEF


def test_parse_flags_rejects_unknown_values() -> None:
    assert parse_flags(None) == ()
    with pytest.raises(ValueError):
        parse_flags(["scary"])
