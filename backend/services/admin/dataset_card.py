"""The dataset card: how a rating *study* on one dataset is run (#96).

The card is stored on the `datasets` row in the `card_*` columns and set by
editing the dataset (`POST`/`PATCH /admin/datasets`). Nothing seeds it: the
vendored roster in `dataset_catalog.py` is a frozen backfill of names and
waves, not a card sync.

`None` means "not declared", which readiness treats as a gap. Readiness has
two levels: a *launchable* card declares how its studies are named and
described; a *complete* one also carries the economics, the estimated
completion time and reward. Those stay optional at onboarding, since the
pilot form asks for them anyway.

Deliberately absent, and why:

* The rater-facing prose — instructions, prompt prefix/suffix, system prompt,
  Prolific pool — describes what the dataset *is* and how it must be
  presented. It reaches the experiment through the export's `dataset_meta`,
  which `services/admin/uploads.py` applies at upload. Holding a second copy
  here would not merely duplicate it: the card is copied onto the experiment
  at create, strictly before any upload, so the export's authoritative value
  would arrive to find the field already filled and be discarded as a
  `meta_conflict`.
* The assistance models and the per-dataset tool set change between waves,
  so the pipeline's per-wave config owns them. The models travel the same
  road as the prose: the export stamps them into `dataset_meta`, one per
  method under `assistance_models`, and the upload pins them into
  `assistance_params` under the same key.

What is left is what the pipeline has no opinion on: study naming, the
Prolific-facing blurb, and the study's economics.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass

from models import Dataset
from schemas import DatasetCardFields

# Every card field, in card vocabulary; the column is `card_<field>`. Derived
# from the API schema so a field added there cannot be forgotten here — a
# field with no column then fails on the first read rather than silently
# never being stored.
CARD_FIELDS: tuple[str, ...] = tuple(DatasetCardFields.model_fields)

# Card fields stored as JSON in a Text column, mirroring `Dataset.waves`.
# NULL means undeclared; '[]' is a real declaration of "none".
_JSON_CARD_FIELDS = ("screeners",)

# Mandatory before a *study* launches: a dataset missing any of these is not
# launchable. Per Joshua on #96 none of this is needed for headroom analysis —
# an unfinished card still uploads and analyses.
#
# Only fields this card owns can be required *of it*. The rater instructions
# and the prompt prefix/suffix are equally mandatory before a launch, but they
# arrive with the upload, so the gate that asks for them reads the experiment
# row instead.
LAUNCH_REQUIRED_FIELDS: tuple[str, ...] = (
    "external_study_name",
    "internal_study_name",
    # The Prolific-facing blurb. Mandatory per Joshua on #96: "for the
    # per-ExperimentRound description, this can be a mandatory field".
    "study_blurb",
)

# What a *complete* card adds on top: the study's economics. Optional at
# onboarding, since the pilot form asks for them anyway. But a dataset is
# not complete until both are on its card.
COMPLETE_REQUIRED_FIELDS: tuple[str, ...] = (
    *LAUNCH_REQUIRED_FIELDS,
    "estimated_completion_time",
    "reward",
)


def missing_fields(values: Mapping[str, object], required: tuple[str, ...]) -> list[str]:
    """Fields of `required` that are undeclared, in `required` order.

    Undeclared means `None`, or a string that is blank once stripped. An empty
    collection is *not* missing — `screeners=[]` is a declaration of "none".
    """
    missing: list[str] = []
    for name in required:
        value = values.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(name)
    return missing


@dataclass(frozen=True)
class CardReadiness:
    """How far a card is filled in: launchable, then complete.

    `missing_for_complete` lists everything a complete card still lacks, so it
    always includes `missing_for_launch`.
    """

    missing_for_launch: list[str]
    missing_for_complete: list[str]

    @property
    def launch_ready(self) -> bool:
        return not self.missing_for_launch

    @property
    def complete(self) -> bool:
        return not self.missing_for_complete


def card_readiness(values: Mapping[str, object]) -> CardReadiness:
    """Readiness of card values given in card vocabulary."""
    return CardReadiness(
        missing_for_launch=missing_fields(values, LAUNCH_REQUIRED_FIELDS),
        missing_for_complete=missing_fields(values, COMPLETE_REQUIRED_FIELDS),
    )


def _card_column(name: str) -> str:
    return f"card_{name}"


def card_values_from_row(dataset: Dataset) -> dict[str, object]:
    """Card field values read off a `datasets` row, in card vocabulary."""
    values: dict[str, object] = {}
    for name in CARD_FIELDS:
        raw = getattr(dataset, _card_column(name))
        values[name] = json.loads(raw) if name in _JSON_CARD_FIELDS and raw is not None else raw
    return values


def write_card_values(dataset: Dataset, values: Mapping[str, object]) -> None:
    """Write card values onto a row.

    Only card fields present in `values` are touched, so a partial PATCH
    leaves the rest of the card alone, and an explicit `None` clears one.
    """
    for name in CARD_FIELDS:
        if name not in values:
            continue
        value = values[name]
        if name in _JSON_CARD_FIELDS and value is not None:
            value = json.dumps(list(value))
        setattr(dataset, _card_column(name), value)


def dataset_readiness(dataset: Dataset) -> CardReadiness:
    """Readiness of a stored dataset row, for `GET /admin/datasets`."""
    return card_readiness(card_values_from_row(dataset))
