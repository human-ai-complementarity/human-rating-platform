"""Study flags and everything that follows from them, in one place.

An experiment carries a set of flags describing what raters are exposed to.
Each flag maps independently onto Prolific's study settings, the prescreener
it requires, which consent statement raters see, and whether the study ends
on a debrief. Adding a flag (or a document for one) is a new row here and
nothing else.

Sources:
- content warnings: https://researcher-help.prolific.com/en/articles/445152-how-do-i-run-a-study-with-sensitive-or-disturbing-content
- deception: https://researcher-help.prolific.com/en/articles/445176-can-i-run-studies-that-deceive-participants
- prescreener ids and choice values: GET https://api.prolific.com/api/v1/filters/
  ("harmful-content": {"0": "Yes", "1": "No"}; "deception": {"0": "Yes, I would
  be comfortable to take part in such a study", "1": "No, ..."}).
"""

from __future__ import annotations

from dataclasses import dataclass

from models import StudyFlag

from .source import CONSENT_STANDARD, CONSENT_WARNED, DEBRIEF


@dataclass(frozen=True)
class FlagEffect:
    # Prolific's `content_warnings` value, if the flag sets one.
    content_warning: str | None
    # Screener key (see services.admin.prolific.SCREENER_FILTERS) every round
    # of such a study must carry.
    prescreener: str | None
    # Which consent statement raters see when this flag is set.
    consent_document: str
    # Whether the study must end on the debrief statement.
    debrief: bool


FLAG_EFFECTS: dict[StudyFlag, FlagEffect] = {
    StudyFlag.SENSITIVE: FlagEffect(
        content_warning="sensitive",
        prescreener="harmful_content",
        consent_document=CONSENT_WARNED,
        debrief=False,
    ),
    StudyFlag.EXPLICIT: FlagEffect(
        content_warning="explicit",
        prescreener="harmful_content",
        consent_document=CONSENT_WARNED,
        debrief=False,
    ),
    StudyFlag.DECEPTION: FlagEffect(
        content_warning=None,
        prescreener="deception",
        consent_document=CONSENT_STANDARD,
        debrief=True,
    ),
}

# Prolific accepts a single content warning per study today ("mutually
# exclusive ... only a single option can be selected"). When both are set the
# stronger one is sent.
_CONTENT_WARNING_PRECEDENCE = ("explicit", "sensitive")


@dataclass(frozen=True)
class StudyRequirements:
    """Everything the rest of the platform needs to know about a flag set."""

    flags: tuple[StudyFlag, ...]
    # The warning sent to Prolific (and shown in the study description), or None.
    content_warning: str | None
    # Screener keys forced onto every round, in a stable order.
    prescreeners: tuple[str, ...]
    consent_document: str
    # The debrief document, or None when the study ends normally.
    debrief_document: str | None

    @property
    def warned(self) -> bool:
        return self.content_warning is not None


def parse_flags(values: list[str] | tuple[str, ...] | None) -> tuple[StudyFlag, ...]:
    """Validate and normalise a stored or submitted flag list: known values,
    deduplicated, in declaration order."""
    seen: list[StudyFlag] = []
    for value in values or ():
        flag = StudyFlag(value)
        if flag not in seen:
            seen.append(flag)
    return tuple(sorted(seen, key=list(StudyFlag).index))


def requirements_for(flags: list[str] | tuple[str, ...] | None) -> StudyRequirements:
    parsed = parse_flags(flags)
    effects = [FLAG_EFFECTS[flag] for flag in parsed]
    warnings = {effect.content_warning for effect in effects if effect.content_warning}
    content_warning = next((w for w in _CONTENT_WARNING_PRECEDENCE if w in warnings), None)
    prescreeners = tuple(dict.fromkeys(e.prescreener for e in effects if e.prescreener))
    consent_document = (
        CONSENT_WARNED
        if any(e.consent_document == CONSENT_WARNED for e in effects)
        else CONSENT_STANDARD
    )
    return StudyRequirements(
        flags=parsed,
        content_warning=content_warning,
        prescreeners=prescreeners,
        consent_document=consent_document,
        debrief_document=DEBRIEF if any(e.debrief for e in effects) else None,
    )
