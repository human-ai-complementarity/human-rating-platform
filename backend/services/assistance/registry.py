"""Registry mapping method names to AssistanceMethod classes.

## What is an assistance method?

An assistance method is a configurable way to support raters while they answer
questions. Methods are configured per-experiment, so different experiments can
test different approaches and compare their effect on rating quality.

## Adding a new method

1. Create a file in services/assistance/methods/, e.g. methods/hint.py
2. Define a class that extends AssistanceMethod and implements start():

    class HintMethod(AssistanceMethod):
        async def start(self, question: Question, params: dict) -> InteractionStep:
            hint = await generate_hint(question.question_text, params)
            return InteractionStep(type=StepType.DISPLAY, payload={"hint": hint}, is_terminal=True)

3. Register it here:

    from .methods.hint import HintMethod
    _REGISTRY["hint"] = HintMethod

4. Configure an experiment to use it by setting assistance_method="hint" on
   ExperimentCreate (and optionally assistance_params for method-specific config).

## One-shot vs multi-turn methods

One-shot methods (hints, evidence display, search results) only implement
start(), which returns a terminal step immediately.

Multi-turn methods (AI chat, human-as-a-tool) also implement advance(), which
is called each time the rater submits input. The interaction continues until
advance() returns a step with is_terminal=True.

## Step types

    StepType.NONE       No assistance produced. Terminal.
    StepType.DISPLAY    Show content to the rater. Terminal.
    StepType.ASK_INPUT  Ask the rater a question, then call advance(). Not terminal.
    StepType.COMPLETE   Multi-turn interaction finished. Terminal.

## Payload vs state

InteractionStep carries two separate dicts:

    step.payload  — sent to the frontend as AssistanceStepResponse.payload.
                    Only include what the UI needs to render.

    step.state    — persisted in AssistanceSession.state and passed back to
                    advance() on the next call. Never sent to the client.
                    Can hold internal memory (full conversation history,
                    subtask list, intermediate reasoning, etc.) without
                    leaking it to the rater or inflating the API response.

For one-shot (terminal) methods, state can be left empty — there is no next
turn to resume.

## What will evolve as methods get richer

The payload/state split and params snapshotting are stable infrastructure.
What is expected to evolve:

- payload is currently an untyped dict. There is no enforced contract between
  what a method puts in payload and what its frontend component expects to
  render. This is fine for the methods we have, but richer methods will likely
  push toward typed payload schemas per method.

- The advance() signature (state, human_input: str, params) may need to
  broaden. A richer method might want structured rater input rather than a
  plain string. Expect the first AI-assisted method to force this.
"""

from __future__ import annotations

from .base import AssistanceMethod
from .methods.human_as_a_tool import HumanAsAToolMethod
from .methods.none import NoAssistance
from .methods.top_n import TopNAssistance

_REGISTRY: dict[str, type[AssistanceMethod]] = {
    "none": NoAssistance,
    "human_as_a_tool": HumanAsAToolMethod,
    "top_n": TopNAssistance,
}


def get_method(name: str) -> AssistanceMethod:
    """Return an instantiated AssistanceMethod for the given name.

    Raises ValueError for unknown method names so callers get a clear error
    rather than an AttributeError deep in the stack.
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown assistance method {name!r}. Available: {sorted(_REGISTRY)}")
    return cls()


def register(name: str, cls: type[AssistanceMethod]) -> None:
    """Register a new method at runtime (useful for tests or plugins)."""
    _REGISTRY[name] = cls
