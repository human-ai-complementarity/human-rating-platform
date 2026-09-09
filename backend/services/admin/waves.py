"""Wave-token normalization.

Dataset membership lists and group attribution waves must compare as the
same token. One helper so strip/lowercase cannot drift across write and
filter sites. Lives here (not datasets.py) so groups can import it without
a datasets ↔ groups cycle.
"""

from __future__ import annotations


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
