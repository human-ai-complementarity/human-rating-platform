"""Dataset card readiness (#96).

Two levels: a *launchable* card can launch a study; a *complete* one also
carries the economics (estimated completion time and reward), which stay
optional at onboarding.
"""

from __future__ import annotations

from models import Dataset
from services.admin.dataset_card import (
    CARD_FIELDS,
    COMPLETE_REQUIRED_FIELDS,
    LAUNCH_REQUIRED_FIELDS,
    card_readiness,
    dataset_readiness,
    missing_fields,
    write_card_values,
)
from services.admin.uploads import DATASET_META_KEYS

_LAUNCHABLE = {
    "external_study_name": "Reading comprehension rating",
    "internal_study_name": "demo fall25",
    "study_blurb": "Rate short passages for factual accuracy.",
}
_ECONOMICS = {"estimated_completion_time": 30, "reward": 450}


def _dataset(**values) -> Dataset:
    dataset = Dataset(name="demo")
    write_card_values(dataset, values)
    return dataset


def test_a_bare_dataset_reports_every_missing_field_at_both_levels():
    readiness = dataset_readiness(_dataset())
    assert not readiness.launch_ready
    assert not readiness.complete
    assert readiness.missing_for_launch == list(LAUNCH_REQUIRED_FIELDS)
    assert readiness.missing_for_complete == list(COMPLETE_REQUIRED_FIELDS)


def test_a_card_without_economics_is_launchable_but_not_complete():
    """Economics are optional at onboarding; the pilot form asks for them."""
    readiness = dataset_readiness(_dataset(**_LAUNCHABLE))
    assert readiness.launch_ready
    assert readiness.missing_for_launch == []
    assert not readiness.complete
    assert readiness.missing_for_complete == ["estimated_completion_time", "reward"]


def test_a_card_with_economics_is_complete():
    readiness = dataset_readiness(_dataset(**_LAUNCHABLE, **_ECONOMICS))
    assert readiness.launch_ready
    assert readiness.complete
    assert readiness.missing_for_complete == []


def test_economics_alone_do_not_make_a_card_launchable():
    readiness = card_readiness({**_ECONOMICS, "study_blurb": "A blurb."})
    assert not readiness.launch_ready
    assert readiness.missing_for_launch == ["external_study_name", "internal_study_name"]
    assert readiness.missing_for_complete == ["external_study_name", "internal_study_name"]


def test_complete_is_launchable_plus_economics():
    assert COMPLETE_REQUIRED_FIELDS[: len(LAUNCH_REQUIRED_FIELDS)] == LAUNCH_REQUIRED_FIELDS
    assert set(COMPLETE_REQUIRED_FIELDS) - set(LAUNCH_REQUIRED_FIELDS) == {
        "estimated_completion_time",
        "reward",
    }


def test_blank_and_whitespace_text_counts_as_missing():
    assert "study_blurb" in missing_fields(
        {**_LAUNCHABLE, "study_blurb": "   "}, LAUNCH_REQUIRED_FIELDS
    )
    assert "study_blurb" in missing_fields(
        {**_LAUNCHABLE, "study_blurb": ""}, LAUNCH_REQUIRED_FIELDS
    )


def test_the_card_does_not_carry_what_the_pipeline_stamps_into_the_export():
    """#96: the export's `dataset_meta` owns the dataset's presentation.

    Not merely a duplicate. This card is copied onto the experiment at create,
    strictly before any upload exists, and `_apply_meta_to_experiment` never
    overwrites a populated value — so a copy here would make the authoritative
    value from the export arrive too late and be discarded as a conflict.
    Requiring any of these *of the card* would be the same mistake.

    `DATASET_META_KEYS`, not just the column-backed five: the wave's assistance
    models travel the same road, and an `assistance_models` field added here
    would spring exactly the same trap.
    """
    assert set(CARD_FIELDS).isdisjoint(DATASET_META_KEYS)
    assert set(COMPLETE_REQUIRED_FIELDS).isdisjoint(DATASET_META_KEYS)


def test_every_card_field_has_a_column():
    """CARD_FIELDS comes from the API schema; each needs a `card_` column."""
    columns = {name for name in Dataset.model_fields if name.startswith("card_")}
    assert columns == {f"card_{name}" for name in CARD_FIELDS}
