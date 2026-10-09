"""Which model each assistance call runs on (#95, #96, #148).

One resolver instead of an ad-hoc `params.get(...) or settings.llm.<x>` chain
repeated at four call sites, so the precedence cannot drift.

Every role a method calls a model for runs the model *under test* (#148): the
method's entry in `assistance_params["assistance_models"]` (the wave's model,
pinned by the question upload's `dataset_meta`), else the platform default for
that role in `LLMSettings`.

Confidence and clustering keep an override (`confidence_model`,
`clustering_model`) that beats the map: an escape hatch for running them on
another model. Each role resolves independently: overriding one never moves
another.

Nothing here knows about datasets or waves: the wave's models are pinned once,
when the pipeline's export is uploaded, and land in `assistance_params`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from fastapi import HTTPException

from config import get_settings

from .llm import model_prefixes

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


def _override_key(role: str) -> str:
    return f"{role}_model"


def instrument_model_keys() -> tuple[str, ...]:
    """The `<role>_model` overrides, one per role in `_OVERRIDE_ROLES`.

    Derived, so a role given an override is validated with no edit here.
    """
    return tuple(_override_key(role) for role in MODEL_ROLES if role in _OVERRIDE_ROLES)


# The example prefix in error messages; `llm.parse_model` accepts every
# registered provider's.
MODEL_PREFIX = "openrouter/"

# `assistance_params` key holding the wave's per-method models, e.g.
# {"top_n": "openrouter/...", "human_as_a_tool": "openrouter/..."}.
ASSISTANCE_MODELS_KEY = "assistance_models"

# The removed single-model key: it silently beat `assistance_models`.
REMOVED_MODEL_KEY = "model"

# Where a session's params snapshot records, per role its method uses, the
# model that role is set to call, e.g. {"decomposition": ..., "confidence": ...}.
# A session may end before calling a role, and failed sessions are recorded
# too, so filter on `step_type`. A record only: nothing resolves a model from it.
ROLE_MODELS_KEY = "role_models"


# Where a resolved model came from: the experiment's map, the role's own
# `<role>_model` override, or the platform default.
ModelSource = Literal["assistance_models", "override", "default"]


def role_default(role: str) -> str:
    """The platform default for `role`, from `LLMSettings`."""
    return getattr(get_settings().llm, _ROLE_DEFAULTS[role])


def pinned_model(params: Mapping[str, object], method: str) -> str | None:
    """`method`'s `assistance_models` entry; None leaves the default."""
    models = params.get(ASSISTANCE_MODELS_KEY)
    if isinstance(models, Mapping) and models.get(method):
        return str(models[method])
    return None


def resolve_model_and_source(
    params: Mapping[str, object], method: str, role: str
) -> tuple[str, ModelSource]:
    """The model `method` runs `role` on under `assistance_params`, and its source.

    The one resolution path: the calls themselves (via `resolve_model`) and
    the admin display (`registry.resolved_models`) both come through here.
    """
    if role in _OVERRIDE_ROLES:
        override = params.get(_override_key(role))
        if override:
            return str(override), "override"
    pinned = pinned_model(params, method)
    if pinned:
        return pinned, "assistance_models"
    return role_default(role), "default"


def resolve_model(params: Mapping[str, object], method: str, role: str) -> str:
    """The model `method` runs `role` on, given an experiment's `assistance_params`."""
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
                f'{{"top_n": "{MODEL_PREFIX}anthropic/claude-sonnet-4.6"}}.'
                + (f" {hint}" if hint else "")
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

    Catches malformed ids, not unreachable ones: the providers accept arbitrary
    model names, so `openrouter/anthropic/claude-sonnet-4-7` passes here and
    only fails when it is actually called.
    """
    prefixes = model_prefixes()
    if not any(model.startswith(prefix) and len(model) > len(prefix) for prefix in prefixes):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid model {model!r} in {field}. Expected "
                f"'<provider>/<model-id>' with provider one of "
                f"{', '.join(p.rstrip('/') for p in prefixes)}, e.g. "
                f"'{MODEL_PREFIX}anthropic/claude-sonnet-4-6'."
            ),
        )
