"""Rendering study names from a dataset card's templates (#96).

Division of labour, because it is easy to get backwards: **the card's
templates differentiate experiments** (dataset x wave x arm); `rounds.py`'s
`_build_round_study_name` / `_build_round_internal_name` already differentiate
*rounds* by appending " - Pilot" / " - Round N". So a template must not end
in that suffix — it is already handled — and must leave room for it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from fastapi import HTTPException

# Closed whitelist for the internal name. Substitution is explicit rather than
# `str.format` because format strings over author-supplied text are a footgun
# ("{0.__class__}"), and a typo like "{waves}" must be a clean 400 rather than
# a 500.
PLACEHOLDERS = ("dataset", "wave", "method")
# The public name takes none. Participants read it in the Prolific title and
# the rater header, so it must not tell them the dataset, wave or arm.
EXTERNAL_PLACEHOLDERS: tuple[str, ...] = ()

# Matches the Experiment.name / internal_name String(255) columns.
NAME_MAX_LENGTH = 255
# The longest suffix rounds.py appends downstream (" - Round 99"). Reserved so
# a template that fits here still fits after the round suffix — nothing
# truncates before the name reaches Prolific, and an overlong name comes back
# as an opaque 502 at the worst possible moment.
ROUND_SUFFIX_RESERVE = len(" - Round 99")
TEMPLATE_MAX_LENGTH = NAME_MAX_LENGTH - ROUND_SUFFIX_RESERVE

_TOKEN = re.compile(r"\{([^{}]*)\}")
# Only the suffix rounds.py appends, and only at the end, so "Co-pilot
# evaluation" or "two-round debate" are fine.
_ROUND_SUFFIX = re.compile(r" - (?:Pilot|Round \d+)\s*$")


def assert_no_round_suffix(template: str, field: str) -> None:
    """Reject a template that ends in its own round suffix."""
    if _ROUND_SUFFIX.search(template):
        raise HTTPException(
            status_code=400,
            detail=(
                f'{field} must not end in " - Pilot" or " - Round N": the round suffix is '
                f'appended when each study is created, so "{template}" would render as '
                f'"{template} - Pilot".'
            ),
        )


def check_template(
    template: str,
    *,
    field: str,
    allowed: tuple[str, ...] = PLACEHOLDERS,
) -> None:
    """400 unless `template` is renderable: no round marker, and only
    whitelisted placeholders.

    Unknown or disallowed placeholders are refused — silently leaving them in
    would ship "{wave}" to Prolific as literal text.
    """
    assert_no_round_suffix(template, field)
    for match in _TOKEN.finditer(template):
        key = match.group(1).strip()
        if key in allowed:
            continue
        if not allowed:
            detail = f'{field} takes no placeholders: participants see it. Remove "{{{key}}}".'
        else:
            known = ", ".join(f"{{{p}}}" for p in allowed)
            detail = f'Unknown placeholder "{{{key}}}" in {field}. Available here: {known}.'
        raise HTTPException(status_code=400, detail=detail)


def render_study_name(
    template: str,
    *,
    context: dict[str, str],
    field: str,
    allowed: tuple[str, ...] = PLACEHOLDERS,
) -> str:
    """Check the template, substitute its placeholders, then trim to fit."""
    check_template(template, field=field, allowed=allowed)
    rendered = _TOKEN.sub(lambda match: context.get(match.group(1).strip(), ""), template)
    # Collapse the gaps a blank substitution leaves behind.
    rendered = re.sub(r"\s{2,}", " ", rendered).strip()
    if not rendered:
        raise HTTPException(
            status_code=400,
            detail=f"{field} rendered empty from template {template!r}.",
        )
    return rendered[:TEMPLATE_MAX_LENGTH].rstrip()


def check_card_templates(values: Mapping[str, object]) -> None:
    """Check the card's name templates when they are saved.

    Rendering happens only at experiment create, so without this a bad
    template would sit on the card until a create tripped over it.
    """
    for field, allowed in (
        ("external_study_name", EXTERNAL_PLACEHOLDERS),
        ("internal_study_name", PLACEHOLDERS),
    ):
        template = values.get(field)
        if isinstance(template, str):
            check_template(template, field=field, allowed=allowed)


def disambiguate(name: str, taken: set[str]) -> str:
    """Append " (2)", " (3)" ... until `name` is unused.

    `experiments.name` has no unique constraint, so two arms of the same group
    rendered from one template would otherwise be byte-identical and
    indistinguishable in Prolific's study list.
    """
    if name not in taken:
        return name
    n = 2
    while True:
        suffix = f" ({n})"
        candidate = name[: TEMPLATE_MAX_LENGTH - len(suffix)].rstrip() + suffix
        if candidate not in taken:
            return candidate
        n += 1
