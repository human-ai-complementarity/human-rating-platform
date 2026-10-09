"""The model an assistance call runs on, and the request options it runs with.

Each assisted method's entry in `assistance_params["assistance_models"]` is an
object shaped like an arm in the inference pipeline's wave config:

    {"model": "openai/gpt-5.6-luna", "reasoning_effort": "low",
     "text_verbosity": "low", "temperature": null}

Every key is required and null means "do not send", so what a rater's
assistant ran with is declared, never inferred from a model family or left to
a provider default. A method without an entry runs on the platform default,
which each method spells out the same way (`default_assistance_model`).

Validation happens on upload and on experiment create/PATCH. Catching a bad
entry early matters more than it looks: the exception `llm.parse_model` raises
at call time is swallowed by every assistance method into a `StepType.NONE`
step, so a malformed id does not fail loudly — it produces a study where every
rater silently got no assistance, indistinguishable afterwards from one where
the model had nothing to offer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from fastapi import HTTPException

from .llm import REASONING_EFFORTS, TEXT_VERBOSITIES, model_prefixes

# The example prefix in error messages.
MODEL_PREFIX = "openrouter/"

# `assistance_params` key holding the wave's per-method entries.
ASSISTANCE_MODELS_KEY = "assistance_models"

# The removed single-model key: it silently beat `assistance_models`.
REMOVED_MODEL_KEY = "model"

# The keys of one entry, all required.
ENTRY_KEYS = ("model", "reasoning_effort", "text_verbosity", "temperature")

EXAMPLE_ENTRY = (
    f'{{"model": "{MODEL_PREFIX}anthropic/claude-sonnet-4-6", "reasoning_effort": null, '
    '"text_verbosity": null, "temperature": 0}'
)


@dataclass(frozen=True)
class AssistanceModel:
    """One method's model and the options every call to it is sent with."""

    model: str
    reasoning_effort: str | None = None
    text_verbosity: str | None = None
    temperature: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """The stored entry shape; also the keyword arguments for `llm.complete`."""
        return asdict(self)

    @classmethod
    def from_entry(cls, entry: Any, default: AssistanceModel) -> AssistanceModel:
        """Read a stored entry leniently, never failing a rater.

        Writes are validated, so a well-formed object is the normal case. A
        bare string (a session snapshot or row written before entries became
        objects) keeps the method's default options, which is what it ran
        with then; an object missing a key gets that key's default.
        """
        if isinstance(entry, str):
            return cls(model=entry, **{k: getattr(default, k) for k in ENTRY_KEYS[1:]})
        values = {k: entry.get(k, getattr(default, k)) for k in ENTRY_KEYS}
        return cls(**values)


# Where a resolved model came from: the experiment's map, or the method default.
ModelSource = Literal["assistance_models", "default"]


def _is_entry(value: Any) -> bool:
    """A stored value that names a model: a well-formed object or a legacy string."""
    if isinstance(value, str):
        return bool(value)
    return isinstance(value, dict) and isinstance(value.get("model"), str) and bool(value["model"])


def resolve_assistance_model_and_source(
    params: dict, method: str, default: AssistanceModel
) -> tuple[AssistanceModel, ModelSource]:
    """`resolve_assistance_model`, plus which of the two it used."""
    models = params.get(ASSISTANCE_MODELS_KEY)
    if isinstance(models, dict) and _is_entry(models.get(method)):
        return AssistanceModel.from_entry(models[method], default), "assistance_models"
    return default, "default"


def resolve_assistance_model(
    params: dict, method: str, default: AssistanceModel
) -> AssistanceModel:
    """What `method` runs on and with: its `assistance_models` entry, else `default`."""
    return resolve_assistance_model_and_source(params, method, default)[0]


# Appended on create/PATCH: an admin page loaded before `model` was removed
# re-sends the stored params, old key included, on every save.
RELOAD_HINT = (
    "If you opened this admin page before the update, reload it: its saves still send the old key."
)


def reject_removed_model_key(values: dict, *, where: str, hint: str = "") -> None:
    """400 when `values` still carries the removed `model` key."""
    if REMOVED_MODEL_KEY in values:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{REMOVED_MODEL_KEY!r} is no longer supported in {where}; use "
                f"{ASSISTANCE_MODELS_KEY!r}, which sets each method's model, e.g. "
                f'{{"top_n": {EXAMPLE_ENTRY}}}.' + (f" {hint}" if hint else "")
            ),
        )


def _reject(field: str, problem: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail=f"Invalid model entry in {field}: {problem} Expected e.g. {EXAMPLE_ENTRY}.",
    )


def validate_model_entry(value: Any, *, field: str) -> None:
    """Reject anything but a complete, well-typed entry object, as a 400.

    Strict on purpose: a model declared without its options would run on
    whatever the provider defaults to, which is a research setting nobody
    chose. A bare model string is refused for the same reason. The model id
    check catches malformed ids, not unreachable ones: the providers accept
    arbitrary model names, so `openrouter/anthropic/claude-sonnet-4-7` passes
    here and only fails when it is actually called.
    """
    if isinstance(value, str):
        raise _reject(field, "a bare model id is no longer accepted; declare an object.")
    if not isinstance(value, dict):
        raise _reject(field, "must be a JSON object.")
    missing = [k for k in ENTRY_KEYS if k not in value]
    if missing:
        raise _reject(field, f"missing required key(s) {', '.join(missing)}.")
    unknown = sorted(set(value) - set(ENTRY_KEYS))
    if unknown:
        raise _reject(field, f"unknown key(s) {', '.join(unknown)}.")
    model = value["model"]
    prefixes = model_prefixes()
    if not isinstance(model, str) or not any(
        model.startswith(prefix) and len(model) > len(prefix) for prefix in prefixes
    ):
        raise _reject(
            field,
            f"'model' must be '<provider>/<model-id>' with provider one of "
            f"{', '.join(p.rstrip('/') for p in prefixes)}.",
        )
    _validate_choice(value["reasoning_effort"], REASONING_EFFORTS, field, "reasoning_effort")
    _validate_choice(value["text_verbosity"], TEXT_VERBOSITIES, field, "text_verbosity")
    temperature = value["temperature"]
    if temperature is not None and (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not 0 <= temperature <= 2
    ):
        raise _reject(field, "'temperature' must be null or a number from 0 to 2.")


def _validate_choice(value: Any, allowed: tuple[str, ...], field: str, key: str) -> None:
    if value is not None and value not in allowed:
        raise _reject(field, f"'{key}' must be null or one of {', '.join(allowed)}.")
