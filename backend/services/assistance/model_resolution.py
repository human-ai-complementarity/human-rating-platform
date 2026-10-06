"""Validation for the model an assistance call runs on.

OpenRouter is the only transport, and `llm._parse_model` rejects anything
without its prefix. Catching that early matters more than it looks: the
exception it raises is swallowed by every assistance method into a
`StepType.NONE` step, so a malformed id does not fail loudly — it produces a
study where every rater silently got no assistance, indistinguishable
afterwards from one where the model had nothing to offer.
"""

from __future__ import annotations

from fastapi import HTTPException

# The prefix `llm._parse_model` requires.
MODEL_PREFIX = "openrouter/"

# `assistance_params` key holding the wave's per-method models, e.g.
# {"top_n": "openrouter/...", "human_as_a_tool": "openrouter/..."}.
ASSISTANCE_MODELS_KEY = "assistance_models"

# The removed single-model key: it silently beat `assistance_models`.
REMOVED_MODEL_KEY = "model"


def resolve_model(params: dict, method: str, default: str) -> str:
    """The model `method` runs on: its `assistance_models` entry, else `default`."""
    models = params.get(ASSISTANCE_MODELS_KEY)
    if isinstance(models, dict) and models.get(method):
        return models[method]
    return default


def reject_removed_model_key(values: dict, *, where: str) -> None:
    """400 when `values` still carries the removed `model` key."""
    if REMOVED_MODEL_KEY in values:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{REMOVED_MODEL_KEY!r} is no longer supported in {where}; use "
                f"{ASSISTANCE_MODELS_KEY!r}, which sets each method's model, e.g. "
                f'{{"top_n": "{MODEL_PREFIX}anthropic/claude-sonnet-4.6"}}.'
            ),
        )


def validate_model_id(model: str, *, field: str) -> None:
    """Reject a model id the transport cannot parse, as a 400.

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
