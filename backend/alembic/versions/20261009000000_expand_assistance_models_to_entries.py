"""Expand each assistance_models value from a model id into an entry object.

Revision ID: 20261009000000
Revises: 20260929054644
Create Date: 2026-10-09 00:00:00.000000

An entry used to be the model id alone; the request options were hardcoded
in each method. Now the entry declares them: `{"model": ..., "reasoning_effort":
..., "text_verbosity": ..., "temperature": ...}`. Each stored id becomes an
object carrying the options its method sent at the time, so no experiment
changes behaviour: Top-N ran at temperature 0, Human-as-a-Tool sent none.
Null entries (deliberate clears) and non-string values are left alone.
Upload rows keep the metadata their file declared; AssistanceSession
snapshots are read leniently by the methods.

Frozen: does not import application services.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Sequence, Union

from alembic import op
from sqlalchemy import text
from sqlalchemy.engine import Connection

revision: str = "20261009000000"
down_revision: Union[str, Sequence[str], None] = "20260929054644"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

MAP_KEY = "assistance_models"

# What each method sent before entries carried options, as of this revision.
# Any other method name sends nothing, like human_as_a_tool.
NO_OPTIONS = {"reasoning_effort": None, "text_verbosity": None, "temperature": None}
DEFAULT_OPTIONS: dict[str, dict[str, Any]] = {
    "top_n": {**NO_OPTIONS, "temperature": 0},
    "human_as_a_tool": NO_OPTIONS,
}


def expand_entries(params: dict[str, Any]) -> dict[str, Any]:
    """`params` with every string `assistance_models` value made an entry object.

    Returns `params` unchanged (same object) when nothing needs expanding.
    """
    models = params.get(MAP_KEY)
    if not isinstance(models, dict):
        return params
    expanded = {
        method: (
            {"model": value, **DEFAULT_OPTIONS.get(method, NO_OPTIONS)}
            if isinstance(value, str)
            else value
        )
        for method, value in models.items()
    }
    if expanded == models:
        return params
    return {**params, MAP_KEY: expanded}


def collapse_entries(params: dict[str, Any]) -> dict[str, Any]:
    """The reverse: each entry object back to its model id."""
    models = params.get(MAP_KEY)
    if not isinstance(models, dict):
        return params
    collapsed = {
        method: value["model"] if isinstance(value, dict) and "model" in value else value
        for method, value in models.items()
    }
    if collapsed == models:
        return params
    return {**params, MAP_KEY: collapsed}


def _rewrite(connection: Connection, transform, what: str) -> int:
    rows = connection.execute(
        text("SELECT id, assistance_params FROM experiments WHERE assistance_params LIKE :pattern"),
        {"pattern": f'%"{MAP_KEY}"%'},
    ).all()
    changed = 0
    for experiment_id, raw in rows:
        try:
            params = json.loads(raw)
        except ValueError:
            logger.warning("Skipped experiment %s: assistance_params is not JSON", experiment_id)
            continue
        if not isinstance(params, dict):
            continue
        rewritten = transform(params)
        if rewritten is params:
            continue
        connection.execute(
            text("UPDATE experiments SET assistance_params = :params WHERE id = :id"),
            {"params": json.dumps(rewritten), "id": experiment_id},
        )
        changed += 1
    logger.info("%s assistance_models entries for %s experiment(s)", what, changed)
    return changed


def expand_all(connection: Connection) -> int:
    """Apply `expand_entries` to every experiment. Returns rows changed."""
    return _rewrite(connection, expand_entries, "Expanded")


def collapse_all(connection: Connection) -> int:
    """Apply `collapse_entries` to every experiment. Returns rows changed."""
    return _rewrite(connection, collapse_entries, "Collapsed")


def upgrade() -> None:
    expand_all(op.get_bind())


def downgrade() -> None:
    # The old code reads a string per method; an object would fail every call.
    collapse_all(op.get_bind())
