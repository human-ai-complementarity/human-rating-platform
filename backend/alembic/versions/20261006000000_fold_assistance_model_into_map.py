"""Fold assistance_params["model"] into the per-method assistance_models map.

Revision ID: 20261006000000
Revises: 20260924000000
Create Date: 2026-10-06 00:00:00.000000

`model` used to beat every `assistance_models` entry. It is removed, so each
experiment's `model` moves into the map where it keeps choosing the same model
for the experiment's selected method. A draft switched to the other method
afterwards runs on that method's entry or the default, not on `model`.
AssistanceSession snapshots are left alone: Top-N is one-shot, and an
in-flight Human-as-a-Tool session reads its model from session state.

Frozen: does not import application services.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Sequence, Union

from alembic import op
from sqlalchemy import text
from sqlalchemy.engine import Connection

revision: str = "20261006000000"
down_revision: Union[str, Sequence[str], None] = "20260924000000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

# The registry's methods that run on a model, as of this revision.
ASSISTED_METHODS = ("human_as_a_tool", "top_n")
MODEL_KEY = "model"
MAP_KEY = "assistance_models"


def fold_model_into_map(params: dict[str, Any], method: str) -> dict[str, Any]:
    """`params` without `model`, its value moved into `assistance_models`.

    For an assisted method the value overwrites that method's entry (`model`
    won before). For `none` it fills every assisted method with no entry, since
    `model` applied to whichever method was switched on. A null, empty or
    non-string `model` is just dropped. Returns `params` unchanged when it has
    no `model`.
    """
    if MODEL_KEY not in params:
        return params
    folded = dict(params)
    model = folded.pop(MODEL_KEY)
    if not isinstance(model, str) or not model:
        return folded
    stored = folded.get(MAP_KEY)
    models = dict(stored) if isinstance(stored, dict) else {}
    if method in ASSISTED_METHODS:
        models[method] = model
    else:
        for assisted in ASSISTED_METHODS:
            models.setdefault(assisted, model)
    folded[MAP_KEY] = models
    return folded


def fold_model_into_maps(connection: Connection) -> int:
    """Apply `fold_model_into_map` to every experiment. Returns rows changed."""
    rows = connection.execute(
        text(
            "SELECT id, assistance_method, assistance_params FROM experiments "
            "WHERE assistance_params LIKE :pattern"
        ),
        {"pattern": f'%"{MODEL_KEY}"%'},
    ).all()
    changed = 0
    for experiment_id, method, raw in rows:
        try:
            params = json.loads(raw)
        except ValueError:
            logger.warning("Skipped experiment %s: assistance_params is not JSON", experiment_id)
            continue
        if not isinstance(params, dict) or MODEL_KEY not in params:
            continue
        folded = fold_model_into_map(params, method)
        connection.execute(
            text("UPDATE experiments SET assistance_params = :params WHERE id = :id"),
            {"params": json.dumps(folded) if folded else None, "id": experiment_id},
        )
        changed += 1
    logger.info("Folded assistance model into assistance_models for %s experiment(s)", changed)
    return changed


def upgrade() -> None:
    fold_model_into_maps(op.get_bind())


def downgrade() -> None:
    # No-op: the old code resolves the folded map entries to the same models
    # for each experiment's selected method.
    pass
