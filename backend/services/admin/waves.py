"""Wave tokens: normalization, the known waves, and the current one.

Dataset membership lists and group attribution waves must compare as the
same token. One helper so strip/lowercase cannot drift across write and
filter sites. Lives here (not datasets.py) so groups can import it without
a datasets ↔ groups cycle.
"""

from __future__ import annotations

# Every wave the platform knows, oldest first. Append new waves at the end.
# The catalog backfill also matches these tokens in experiment names.
KNOWN_WAVES: tuple[str, ...] = ("fall25", "sp26", "sum26", "fall26")

# The wave collections are being run for now. A constant, not a setting:
# waves change rarely, and the PR that bumps it is the record.
CURRENT_WAVE = "fall26"


def normalize_wave_token(wave: str) -> str:
    """Strip and lowercase a single wave token (e.g. ``Fall25`` → ``fall25``)."""
    return wave.strip().lower()


def normalize_waves(waves: list[str]) -> list[str]:
    """Lowercase and dedupe wave tokens, preserving first-seen order."""
    seen: set[str] = set()
    result: list[str] = []
    for wave in waves:
        token = normalize_wave_token(wave)
        if token and token not in seen:
            seen.add(token)
            result.append(token)
    return result
