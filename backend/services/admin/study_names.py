"""Rendering study names from a dataset card's templates (#96).

Division of labour, because it is easy to get backwards: **the card's
templates differentiate experiments** (dataset x wave x arm); `rounds.py`'s
`_build_round_study_name` / `_build_round_internal_name` already differentiate
*rounds* by appending " - Pilot" / " - Round N". So a template must not carry
round information — that is already handled — and must leave room for the
suffix that gets appended to it later.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from fastapi import HTTPException

# Closed whitelist. Substitution is explicit rather than `str.format` because
# format strings over author-supplied text are a footgun ("{0.__class__}"),
# and a typo like "{waves}" must be a clean 400 rather than a 500.
PLACEHOLDERS = ("dataset", "wave", "method")

# Matches the Experiment.name / internal_name String(255) columns.
NAME_MAX_LENGTH = 255
# The longest suffix rounds.py appends downstream (" - Round 99"). Reserved so
# a template that fits here still fits after the round suffix — nothing
# truncates before the name reaches Prolific, and an overlong name comes back
# as an opaque 502 at the worst possible moment.
ROUND_SUFFIX_RESERVE = len(" - Round 99")
TEMPLATE_MAX_LENGTH = NAME_MAX_LENGTH - ROUND_SUFFIX_RESERVE

_TOKEN = re.compile(r"\{([^{}]*)\}")
_ROUND_SUFFIX = re.compile(r"-\s*(pilot|round)\b", re.IGNORECASE)


def assert_no_round_suffix(template: str, field: str) -> None:
    """Reject a template that carries its own round marker."""
    if _ROUND_SUFFIX.search(template):
        raise HTTPException(
            status_code=400,
            detail=(
                f'{field} must not contain "- Pilot" or "- Round": the round suffix is '
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
        if key not in allowed:
            known = ", ".join(f"{{{p}}}" for p in allowed) or "(none)"
            raise HTTPException(
                status_code=400,
                detail=(f'Unknown placeholder "{{{key}}}" in {field}. Available here: {known}.'),
            )


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


def external_placeholders() -> tuple[str, ...]:
    """Placeholders the rater-visible name may use (#96).

    The arm and wave stay out of it: a participant reads the study name before
    accepting, so naming the arm there tells them which condition they are in
    and risks biasing who self-selects in.
    """
    return ("dataset",)


def check_card_templates(values: Mapping[str, object]) -> None:
    """Check the card's name templates when they are saved.

    Rendering happens only at experiment create, so without this a bad
    template would sit on the card until a create tripped over it.
    """
    for field, allowed in (
        ("external_study_name", external_placeholders()),
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
