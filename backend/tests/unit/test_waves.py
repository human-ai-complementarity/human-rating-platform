from __future__ import annotations

from services.admin.waves import CURRENT_WAVE, KNOWN_WAVES, normalize_wave_token


def test_current_wave_is_a_known_normalized_wave() -> None:
    # Bumping CURRENT_WAVE means adding the wave to KNOWN_WAVES first.
    assert CURRENT_WAVE in KNOWN_WAVES
    assert CURRENT_WAVE == normalize_wave_token(CURRENT_WAVE)
