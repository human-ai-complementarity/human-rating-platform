"""Registry mapping check names to Check classes.

Adding a check: define a subclass of `Check` in this package with a unique
`name`, add it to `_CHECKS`, and list it in `backend/pilot_check.toml`
(the config loader refuses a file that does not list every check).
"""

from __future__ import annotations

from .assistance_health import AssistanceHealth
from .assistance_output import AssistanceOutput
from .assistance_useful import AssistanceUseful
from .base import Check
from .consent import ConsentConfigured
from .instructions import InstructionsCoherent
from .launch_blockers import LaunchBlockers
from .markdown_mode import MarkdownMode
from .question_coherent import QuestionCoherent
from .question_renders import QuestionRenders

# Report order: experiment-level configuration first, then what each sampled
# question looks like, then the assistance on it, then the judge's reads.
_CHECKS: tuple[type[Check], ...] = (
    ConsentConfigured,
    LaunchBlockers,
    MarkdownMode,
    QuestionRenders,
    AssistanceOutput,
    AssistanceHealth,
    InstructionsCoherent,
    QuestionCoherent,
    AssistanceUseful,
)

_REGISTRY: dict[str, type[Check]] = {cls.name: cls for cls in _CHECKS}
assert len(_REGISTRY) == len(_CHECKS), "duplicate check name"


def check_names() -> tuple[str, ...]:
    return tuple(_REGISTRY)


def get_check(name: str) -> Check:
    return _REGISTRY[name]()
