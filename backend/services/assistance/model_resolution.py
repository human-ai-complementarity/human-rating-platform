"""Which model each assistance call runs on (#95, #96).

One resolver instead of an ad-hoc `params.get(...) or settings.llm.<x>` chain
repeated at four call sites, so the precedence cannot drift.

Answer and decomposition run the model *under test*: the method's entry in
`assistance_params["assistance_models"]` (the wave's model, pinned by the
question upload's `dataset_meta`), else the platform default for that role in
`LLMSettings`.

Confidence and clustering are the platform's measurement instrument and do
not inherit the map: each takes its own override (`confidence_model`,
`clustering_model`), else the default. Clustering's default is whatever
confidence resolves to.

Nothing here knows about datasets or waves: the wave's models are pinned once,
when the pipeline's export is uploaded, and land in `assistance_params`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from fastapi import HTTPException

from config import get_settings

# Role -> the LLMSettings attribute that backs it when nothing overrides.
# Clustering has none: it follows whatever confidence resolves to.
_ROLE_DEFAULTS: dict[str, str] = {
    "answer": "default_model",
    "decomposition": "decomposition_model",
    "confidence": "confidence_model",
}

# Roles that run the model *under test*, and so take `pinned_model`. The
# others are the measurement instrument: swapping a pinned frontier model into
# them would silently change the instrument across datasets and multiply cost
# by `num_samples` per subtask per round.
_INHERITING_ROLES = frozenset({"answer", "decomposition"})

# The only `<role>_model` overrides: the instrument's. The inheriting roles
# have none, since `assistance_models` already sets them.
INSTRUMENT_MODEL_KEYS = ("confidence_model", "clustering_model")

# OpenRouter is the only transport; `llm._parse_model` rejects anything else.
MODEL_PREFIX = "openrouter/"

# `assistance_params` key holding the wave's per-method models, e.g.
# {"top_n": "openrouter/...", "human_as_a_tool": "openrouter/..."}.
ASSISTANCE_MODELS_KEY = "assistance_models"

# The removed single-model key: it silently beat `assistance_models`.
REMOVED_MODEL_KEY = "model"

# Where a session's params snapshot records the model that ran. A record only:
# nothing resolves a model from it. Not `model`, which reads as the removed key.
RESOLVED_MODEL_KEY = "resolved_model"


# Where a resolved model came from: the experiment's map, or the method default.
ModelSource = Literal["assistance_models", "default"]


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
    params: Mapping[str, object], method: str, default: str
) -> tuple[str, ModelSource]:
    """The model under test for `method`, plus which of the two it used."""
    pinned = pinned_model(params, method)
    if pinned:
        return pinned, "assistance_models"
    return default, "default"


def resolve_model(params: Mapping[str, object], method: str, role: str) -> str:
    """The model `method` runs `role` on, given an experiment's `assistance_params`."""
    if role in _INHERITING_ROLES:
        return resolve_model_and_source(params, method, role_default(role))[0]
    model = params.get(f"{role}_model")
    if not model and role == "clustering":
        return resolve_model(params, method, "confidence")
    return str(model or role_default(role))


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


def validate_model_id(model: str, *, field: str) -> None:
    """Reject a model id the transport cannot parse, as a 400.

    Worth failing early and loudly: `_parse_model` raises `ValueError`, and
    both assistance methods swallow that into a `NONE` step. A typo would
    otherwise mean every rater silently gets no assistance, producing a study
    that looks completed and is indistinguishable from one where the model
    legitimately had nothing to offer.

    Catches malformed ids, not unreachable ones: OpenRouter accepts arbitrary
    model names, so `openrouter/anthropic/claude-sonnet-4-7` passes here and
    only fails when it is actually called.
    """
    if not model.startswith(MODEL_PREFIX):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid model {model!r} in {field}. Expected "
                f"'{MODEL_PREFIX}<model-id>', e.g. "
                f"'{MODEL_PREFIX}anthropic/claude-sonnet-4-6'."
            ),
        )
