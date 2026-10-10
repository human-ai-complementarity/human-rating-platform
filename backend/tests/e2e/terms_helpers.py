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


def write_terms_source(root: Path, *, consent_version: int = 1, body: str = _DUMMY_CONSENT) -> None:
    """Lay out a minimal terms source: a standard bundle plus a sensitive and
    an explicit one, each with a debrief. Dummy text throughout."""
    (root / "consent" / "standard").mkdir(parents=True, exist_ok=True)
    (root / "consent" / "standard" / f"v{consent_version}.md").write_text(body, encoding="utf-8")
    for level in ("sensitive", "explicit"):
        (root / "consent" / level).mkdir(parents=True, exist_ok=True)
        (root / "consent" / level / "v1.md").write_text(
            f"## Content warning\n\nThis study contains {level} content: "
            "{{content_warning_details}}\n\n" + body,
            encoding="utf-8",
        )
        (root / "debrief" / level).mkdir(parents=True, exist_ok=True)
        (root / "debrief" / level / "v1.md").write_text(
            "## Thank you\n\nTEST DEBRIEF for **{{study_name}}**. If any of this was difficult, "
            "talk to someone. Continue to Prolific when you are ready.\n",
            encoding="utf-8",
        )
    manifest = {
        "schema": 1,
        "bundles": {
            "standard": {
                "label": "Standard",
                "content_warnings": ["none"],
                "consent": consent_version,
            },
            "sensitive": {
                "label": "Sensitive content",
                "content_warnings": ["sensitive"],
                "consent": 1,
                "debrief": 1,
            },
            "explicit": {
                "label": "Explicit content",
                "content_warnings": ["explicit"],
                "consent": 1,
                "debrief": 1,
            },
        },
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
