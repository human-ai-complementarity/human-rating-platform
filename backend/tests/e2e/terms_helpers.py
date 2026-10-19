"""A runtime-generated rater terms source for the e2e suite.

The repository ships no consent statements; tests lay out dummy ones in a
temporary folder and point TERMS__SOURCE_URL at it.
"""

from __future__ import annotations

import json
from pathlib import Path

# Deliberately not a consent statement: just enough structure for the tests.
_DUMMY_CONSENT = """\
## Purpose of the study

TEST TEXT, not a consent statement. The study is **{{study_name}}**.

## What you will do

You have {{session_length}}. Everything you submit is kept.

## Consent

By clicking **I agree** below, you confirm that you are taking part in a test.
"""

_DUMMY_WARNED_CONSENT = (
    "## Content warning\n\nThis study contains sensitive or explicit content: "
    "{{content_warning_details}}\n\n" + _DUMMY_CONSENT
)

_DUMMY_DEBRIEF = """\
## Thank you

TEST DEBRIEF for **{{study_name}}**. Some of what you were told during the study \
was not accurate; here is what was withheld and why. Continue to Prolific when you \
are ready.
"""


def write_terms_source(root: Path, *, consent_version: int = 1, body: str = _DUMMY_CONSENT) -> None:
    """Lay out the three documents a terms source must provide, with the
    standard consent at `consent_version`. Dummy text throughout."""
    (root / "consent" / "standard").mkdir(parents=True, exist_ok=True)
    (root / "consent" / "standard" / f"v{consent_version}.md").write_text(body, encoding="utf-8")
    (root / "consent" / "warned").mkdir(parents=True, exist_ok=True)
    (root / "consent" / "warned" / "v1.md").write_text(_DUMMY_WARNED_CONSENT, encoding="utf-8")
    (root / "debrief").mkdir(parents=True, exist_ok=True)
    (root / "debrief" / "v1.md").write_text(_DUMMY_DEBRIEF, encoding="utf-8")
    manifest = {
        "documents": {
            "consent/standard": consent_version,
            "consent/warned": 1,
            "debrief": 1,
        }
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
