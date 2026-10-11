"""Shared serialization of method results, independent of transaction ownership."""

import json
from datetime import datetime

from .base import InteractionStep


def optional_json(value: dict) -> str | None:
    return json.dumps(value) if value else None


def step_columns(step: InteractionStep, now: datetime) -> dict:
    return {
        "step_type": step.type,
        "payload": optional_json(step.payload),
        "state": optional_json(step.state),
        "is_complete": step.is_terminal,
        "updated_at": now,
    }
