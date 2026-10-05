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


def resolve_model(params: dict, method: str, default: str) -> str:
    """The model `method` runs on.

    An explicit `model` (admin override) wins, then the wave's entry for this
    method in `assistance_models`, then `default`.
    """
    if params.get("model"):
        return params["model"]
    models = params.get(ASSISTANCE_MODELS_KEY)
    if isinstance(models, dict) and models.get(method):
        return models[method]
    return default


def validate_model_id(model: str, *, field: str = "assistance_params.model") -> None:
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
