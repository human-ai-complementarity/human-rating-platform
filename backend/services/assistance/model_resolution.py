"""Which model each assistance call runs on, and with what (#95, #96, #148).

One resolver instead of an ad-hoc `params.get(...) or settings.llm.<x>` chain
repeated at four call sites, so the precedence cannot drift.

Every role a method calls a model for runs the model *under test* (#148): the
method's entry in `assistance_params["assistance_models"]` (the wave's arm,
pinned by the question upload's `dataset_meta`), else the platform default for
that role in `LLMSettings`.

An entry is an object shaped like an arm in the inference pipeline's wave
config, the model plus the request options every call to it is sent with:

    {"model": "oai/gpt-5.6-luna", "reasoning_effort": "low",
     "text_verbosity": "low", "temperature": null, "tools": ["web_search"]}

Every option is declared and null means "do not send", so what assisted the
raters is never left to a provider default. `tools` alone is optional: left
out, it is no tools (an `openrouter/` model can only have web_search). The
options belong to the method's primary role, the model under test. The instrument roles (confidence, clustering) run the
entry's *model* but keep their own options, and never get its tools:
sampling's temperature is an instrument setting, not the arm's.

Confidence and clustering keep an override (`confidence_model`,
`clustering_model`) that beats the map: an escape hatch for running them on
another model. Each role resolves independently: overriding one never moves
another.

Nothing here knows about datasets or waves: the wave's models are pinned once,
when the pipeline's export is uploaded, and land in `assistance_params`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal

from fastapi import HTTPException

from config import get_settings

from .llm import REASONING_EFFORTS, TEXT_VERBOSITIES, parse_model

# Role -> the LLMSettings attribute that backs it when nothing overrides.
_ROLE_DEFAULTS: dict[str, str] = {
    "answer": "default_model",
    "decomposition": "decomposition_model",
    "confidence": "confidence_model",
    "clustering": "clustering_model",
}

# Every role an assistance method can call a model for.
MODEL_ROLES: tuple[str, ...] = tuple(_ROLE_DEFAULTS)

# Roles with a `<role>_model` override that beats `assistance_models`: an
# escape hatch for scoring or clustering on a different model than the one
# under test, validated like any model key. The primary roles have none, since
# their method's `assistance_models` entry already is their setting.
_OVERRIDE_ROLES = frozenset({"confidence", "clustering"})

# The request options a primary role sends when its entry declares none: what
# it always sent before entries carried options. Top-N ranks at temperature 0;
# decomposition sends nothing. The instrument roles are not listed: their
# options belong to the estimator the method builds (sampling at 0.7,
# clustering at 0), so the method fills them in (`HumanAsAToolMethod.role_models`)
# from the same values it passes to the estimator.
_ROLE_OPTIONS: dict[str, dict[str, Any]] = {"answer": {"temperature": 0}}


def _override_key(role: str) -> str:
    return f"{role}_model"


def instrument_model_keys() -> tuple[str, ...]:
    """The `<role>_model` overrides, one per role in `_OVERRIDE_ROLES`.

    Derived, so a role given an override is validated with no edit here.
    """
    return tuple(_override_key(role) for role in MODEL_ROLES if role in _OVERRIDE_ROLES)


# `assistance_params` key holding the wave's per-method entries.
ASSISTANCE_MODELS_KEY = "assistance_models"

# The removed single-model key: it silently beat `assistance_models`.
REMOVED_MODEL_KEY = "model"

# Where a session's params snapshot records, per role its method uses, the
# entry that role is set to call, e.g. {"decomposition": {...}, "confidence":
# {...}}. A session may end before calling a role, and failed sessions are
# recorded too, so filter on `step_type`. A record only: nothing resolves a
# model from it.
ROLE_MODELS_KEY = "role_models"

# An entry's option keys, every one declared. A key added later (`tools`,
# `max_tokens`) goes in OPTIONAL_OPTIONS with the value an entry lacking it
# gets, so an older export stays valid and the change is purely additive.
OPTION_KEYS = ("reasoning_effort", "text_verbosity", "temperature", "tools")
OPTIONAL_OPTIONS: dict[str, Any] = {"tools": ()}
ENTRY_KEYS = ("model", *OPTION_KEYS)

# The tools an entry may give the model under test, named as the inference
# pipeline's answerer names them.
TOOLS = ("web_search", "code_interpreter")

EXAMPLE_ENTRY = (
    '{"model": "openrouter/anthropic/claude-sonnet-4-6", "reasoning_effort": null, '
    '"text_verbosity": null, "temperature": 0}'
)


@dataclass(frozen=True)
class AssistanceModel:
    """A model and the request options every call to it is sent with."""

    model: str
    reasoning_effort: str | None = None
    text_verbosity: str | None = None
    temperature: float | None = None
    # A tuple, not a list, so an entry stays hashable: Top-N memoizes its
    # schema verdicts per entry.
    tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # Stored entries hold a JSON list; a null one reads as no tools.
        object.__setattr__(self, "tools", tuple(self.tools or ()))

    def to_dict(self) -> dict[str, Any]:
        """The stored entry shape, every key declared, `tools` as a JSON list."""
        return {**asdict(self), "tools": list(self.tools)}

    def call_kwargs(self) -> dict[str, Any]:
        """The keyword arguments for `llm.complete`, tools included."""
        return asdict(self)

    @classmethod
    def from_entry(cls, entry: Any, options: Mapping[str, Any] = {}) -> AssistanceModel:
        """Read a stored entry leniently, never failing a rater.

        Writes are validated, so a complete object is the normal case. A bare
        id (a session snapshot or row written before entries carried options)
        and an object missing a key take `options` for what they lack.
        """
        if isinstance(entry, str):
            entry = {"model": entry}
        values = {k: entry.get(k, options.get(k, OPTIONAL_OPTIONS.get(k))) for k in OPTION_KEYS}
        return cls(model=str(entry["model"]), **values)


def same_entry(stored: Any, declared: Any) -> bool:
    """Whether two stored values declare the same entry.

    An optional key left out is its default, so an entry without `tools` and
    one with `"tools": []` are the same declaration, not a conflict.
    """
    if stored == declared:
        return True
    if not (isinstance(stored, Mapping) and isinstance(declared, Mapping)):
        return False
    if "model" not in stored or "model" not in declared:
        return False
    return AssistanceModel.from_entry(stored) == AssistanceModel.from_entry(declared)


# Where a resolved model came from: the experiment's map, the role's own
# `<role>_model` override, or the platform default.
ModelSource = Literal["assistance_models", "override", "default"]


def role_options(role: str) -> dict[str, Any]:
    """The options `role` sends when its entry declares none."""
    return dict(_ROLE_OPTIONS.get(role, {}))


def role_default(role: str) -> AssistanceModel:
    """The platform default for `role`: its `LLMSettings` model, its own options."""
    model = getattr(get_settings().llm, _ROLE_DEFAULTS[role])
    return AssistanceModel.from_entry(model, role_options(role))


def _pinned_value(params: Mapping[str, object], method: str) -> str | Mapping[str, Any] | None:
    """`method`'s raw `assistance_models` entry when it names a model."""
    models = params.get(ASSISTANCE_MODELS_KEY)
    if not isinstance(models, Mapping):
        return None
    value = models.get(method)
    if isinstance(value, str) and value:
        return value
    if isinstance(value, Mapping) and isinstance(value.get("model"), str) and value["model"]:
        return value
    return None


def pinned_model(params: Mapping[str, object], method: str) -> AssistanceModel | None:
    """`method`'s `assistance_models` entry as declared; None leaves the default.

    The launch gate's test (`status.experiment_launch_blockers`), so the gate
    and the model that runs cannot disagree.
    """
    value = _pinned_value(params, method)
    return AssistanceModel.from_entry(value) if value is not None else None


def resolve_model_and_source(
    params: Mapping[str, object], method: str, role: str
) -> tuple[AssistanceModel, ModelSource]:
    """What `method` runs `role` on under `assistance_params`, and its source.

    The one resolution path: the calls themselves (via `resolve_model`) and
    the admin display (`registry.resolved_models`) both come through here.
    """
    options = role_options(role)
    if role in _OVERRIDE_ROLES:
        override = params.get(_override_key(role))
        if override:
            return AssistanceModel.from_entry(str(override), options), "override"
    value = _pinned_value(params, method)
    if value is not None:
        entry = AssistanceModel.from_entry(value, options)
        if role in _OVERRIDE_ROLES:
            # An instrument runs the model under test with its own options,
            # and never with its tools.
            entry = AssistanceModel.from_entry(entry.model, options)
        return entry, "assistance_models"
    return role_default(role), "default"


def resolve_model(params: Mapping[str, object], method: str, role: str) -> AssistanceModel:
    """What `method` runs `role` on, given an experiment's `assistance_params`."""
    return resolve_model_and_source(params, method, role)[0]


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


def reject_role_models_key(values: Mapping[str, object], *, where: str) -> None:
    """400 when `values` sets `role_models`. Sessions write that record; set
    in `assistance_params` it would be copied into each one as if it ran."""
    if ROLE_MODELS_KEY in values:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{ROLE_MODELS_KEY!r} cannot be set in {where}: each assistance session "
                f"records it. Set each method's model with {ASSISTANCE_MODELS_KEY!r}."
            ),
        )


def validate_model_id(model: str, *, field: str) -> None:
    """Reject a model id the transport cannot parse, as a 400.

    Worth failing early and loudly: `parse_model` raises `ValueError`, and
    both assistance methods swallow that into a `NONE` step. A typo would
    otherwise mean every rater silently gets no assistance, producing a study
    that looks completed and is indistinguishable from one where the model
    legitimately had nothing to offer.

    Catches malformed ids, not unreachable ones: the providers accept
    arbitrary model names, so `openrouter/anthropic/claude-sonnet-4-7` passes
    here and only fails when it is actually called.
    """
    try:
        parse_model(model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"In {field}: {exc}") from exc


def _reject(field: str, problem: str) -> HTTPException:
    return HTTPException(
        status_code=400,
        detail=f"Invalid model entry in {field}: {problem} Expected e.g. {EXAMPLE_ENTRY}.",
    )


def validate_model_entry(value: Any, *, field: str) -> None:
    """Reject anything but a complete, well-typed entry object, as a 400.

    Strict on purpose: a model declared without its options would run on
    whatever the provider defaults to, which is a research setting nobody
    chose. A bare model id is refused for the same reason (the question upload
    alone still expands one, for exports written before entries existed).
    """
    if isinstance(value, str):
        raise _reject(field, "a bare model id is no longer accepted; declare an object.")
    if not isinstance(value, dict):
        raise _reject(field, "must be a JSON object.")
    missing = [k for k in ENTRY_KEYS if k not in value and k not in OPTIONAL_OPTIONS]
    if missing:
        raise _reject(field, f"missing required key(s) {', '.join(missing)}.")
    unknown = sorted(set(value) - set(ENTRY_KEYS))
    if unknown:
        raise _reject(field, f"unknown key(s) {', '.join(unknown)}.")
    if not isinstance(value["model"], str):
        raise _reject(field, "'model' must be a string.")
    try:
        parse_model(value["model"])
    except ValueError as exc:
        raise _reject(field, str(exc)) from exc
    _validate_choice(value["reasoning_effort"], REASONING_EFFORTS, field, "reasoning_effort")
    _validate_choice(value["text_verbosity"], TEXT_VERBOSITIES, field, "text_verbosity")
    temperature = value["temperature"]
    if temperature is not None and (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not 0 <= temperature <= 2
    ):
        raise _reject(field, "'temperature' must be null or a number from 0 to 2.")
    tools = value.get("tools", [])
    _validate_tools(tools, field)
    provider, _ = parse_model(value["model"])
    unavailable = [tool for tool in tools if tool not in provider.tools]
    if unavailable:
        raise _reject(
            field,
            f"'tools' has {', '.join(unavailable)}, which {value['model']} cannot use. "
            f"{provider.tool_limit}",
        )


def _validate_choice(value: Any, allowed: tuple[str, ...], field: str, key: str) -> None:
    if value is not None and value not in allowed:
        raise _reject(field, f"'{key}' must be null or one of {', '.join(allowed)}.")


def _validate_tools(tools: Any, field: str) -> None:
    allowed = ", ".join(TOOLS)
    if not isinstance(tools, list):
        raise _reject(
            field,
            f"'tools' must be a list of tool names from {allowed}; omit it or use [] for none.",
        )
    unknown = [tool for tool in tools if tool not in TOOLS]
    if unknown:
        raise _reject(
            field,
            f"'tools' has unknown tool(s) {', '.join(map(repr, unknown))}; allowed: {allowed}.",
        )
    repeated = sorted({tool for tool in tools if tools.count(tool) > 1})
    if repeated:
        raise _reject(field, f"'tools' lists {', '.join(repeated)} more than once.")
