"""Registry mapping method names to AssistanceMethod classes.

## What is an assistance method?

An assistance method is a configurable way to support raters while they answer
questions. Methods are configured per-experiment, so different experiments can
test different approaches and compare their effect on rating quality.

## Adding a new method

1. Create a file in services/assistance/methods/, e.g. methods/hint.py
2. Define a class that extends AssistanceMethod and implements start():

    class HintMethod(AssistanceMethod):
        method_key = "hint"
        primary_model_role = "answer"
        rater_instructions = (
            "For each question, an AI-generated hint will be shown alongside "
            "the question to help you reason through it."
        )

        async def start(self, question: Question, params: dict) -> InteractionStep:
            model = self.role_models(params)[self.primary_model_role]
            hint = await generate_hint(question.question_text, model)
            return InteractionStep(type=StepType.DISPLAY, payload={"hint": hint}, is_terminal=True)

   ``method_key`` is the name experiments select the method by, and keys its
   ``assistance_models`` entry. ``primary_model_role`` is the
   ``model_resolution`` role its rater-facing call runs on; leave it None for
   a method that calls no model. Take models from ``role_models`` (override
   ``model_roles`` if the method calls more than its primary role), never
   literals, so what runs is what the session records.

   ``rater_instructions`` is a short plain-text description of how the method
   works from the rater's perspective, shown on the intro screen before they
   begin rating. It is required on any method that affects the rater
   experience — leave as the empty string only if the method is invisible to
   the rater (e.g. NoAssistance).

3. Register it here, keyed by its ``method_key``:

    from .methods.hint import HintMethod
    _REGISTRY = {cls.method_key: cls for cls in (..., HintMethod)}

4. Configure an experiment to use it by setting assistance_method="hint" on
   ExperimentCreate (and optionally assistance_params for method-specific config).

Preparation is optional. See docs/assistance-prefetch.md for the preparation
contract and extension guide. Use InitialStepPreparation when the entire first
step can run before human input; methods remain responsible for their normal
start() and advance() behavior.

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
from .model_resolution import (
    ROLE_MODELS_KEY,
    AssistanceModel,
    ModelSource,
    resolve_model_and_source,
    role_options,
)
from .methods.human_as_a_tool import HumanAsAToolMethod
from .methods.none import NoAssistance
from .methods.top_n import TopNAssistance

# Keyed by each class's own `method_key`, so the name an experiment selects a
# method by is the one it resolves its models under.
_REGISTRY: dict[str, type[AssistanceMethod]] = {
    cls.method_key: cls for cls in (NoAssistance, HumanAsAToolMethod, TopNAssistance)
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


def _assisted() -> list[tuple[str, type[AssistanceMethod]]]:
    return [(name, cls) for name, cls in sorted(_REGISTRY.items()) if cls.primary_model_role]


def assisted_methods() -> list[str]:
    """Registered methods that run on a model: those with a primary model role."""
    return [name for name, _ in _assisted()]


def expand_bare_entry(name: str, model: str) -> dict:
    """The entry a bare model id stands for on method `name`: that model with
    the options its primary role sends when none are declared.

    For exports written before entries carried options; the question upload
    expands them so the stored map holds one shape.
    """
    role = _REGISTRY[name].primary_model_role
    return AssistanceModel.from_entry(model, role_options(role or "")).to_dict()


def resolved_models(params: dict) -> dict[str, tuple[AssistanceModel, ModelSource]]:
    """Per assisted method, what its primary role would run on under `params`,
    and its source, resolved the same way as the call itself.

    Primary roles only: a map entry runs the other roles too, unless
    overridden (#148). Each session's `role_models` records them all.
    """
    return {
        name: resolve_model_and_source(params, cls.method_key, cls.primary_model_role)
        for name, cls in _assisted()
    }


def record_role_models(name: str, params: dict | None) -> dict:
    """A session's params snapshot: `params` plus, as `role_models`, the model
    each role method `name` calls is called on (`AssistanceMethod.role_models`).

    Shared by the foreground start and the preparation runner, the two places
    a session row is created, so both record the same map. Reads the class
    without instantiating it; an unknown or model-free method records none.
    """
    recorded = dict(params) if params else {}
    # Create/PATCH reject the key; drop any that reached the row another way,
    # so a model-free method never records a model.
    recorded.pop(ROLE_MODELS_KEY, None)
    cls = _REGISTRY.get(name)
    models = cls.role_models(recorded) if cls is not None else {}
    if models:
        recorded[ROLE_MODELS_KEY] = {role: entry.to_dict() for role, entry in models.items()}
    return recorded


def register(name: str, cls: type[AssistanceMethod]) -> None:
    """Register a new method at runtime (useful for tests or plugins)."""
    _REGISTRY[name] = cls


def get_rater_instructions(name: str) -> str:
    """Return the rater-facing instruction text for a method, or "" if none.

    Reads the class attribute without instantiating the method (instantiation
    can be expensive — HumanAsAToolMethod builds a decomposer, etc.). Unknown
    method names return "" so the intro screen simply omits the box.
    """
    cls = _REGISTRY.get(name)
    if cls is None:
        return ""
    return cls.rater_instructions or ""
