from __future__ import annotations

import pytest

from config import Settings
from services.admin.waves import KNOWN_WAVES, current_wave


def test_current_wave_defaults_to_the_newest_known_wave(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WAVES__CURRENT", raising=False)
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret")

    assert current_wave(Settings(_env_file=None)) == KNOWN_WAVES[-1]


def test_current_wave_env_override_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    # A wave the code does not know yet is allowed: the env var is how a new
    # wave starts before KNOWN_WAVES is updated.
    monkeypatch.setenv("WAVES__CURRENT", " SP27 ")
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret")

    assert current_wave(Settings(_env_file=None)) == "sp27"
