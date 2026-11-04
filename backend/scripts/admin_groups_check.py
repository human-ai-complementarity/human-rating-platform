"""Read ADMIN_GROUPS from Google and print who the platform would admit. Writes nothing.

    uv run python scripts/admin_groups_check.py                      # groups from ADMIN_GROUPS
    uv run python scripts/admin_groups_check.py team@example.com

Credentials: GOOGLE_APPLICATION_CREDENTIALS (the platform's key) or your
gcloud login, which needs a quota project where the Cloud Identity API is
enabled: ``gcloud auth application-default set-quota-project <project>``.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import get_settings  # noqa: E402
from services.admin_groups import GroupReadError, google_session, read_group_emails  # noqa: E402

groups = sys.argv[1:] or get_settings().admin_groups
if not groups:
    print("No groups: set ADMIN_GROUPS or pass group emails as arguments", file=sys.stderr)
    raise SystemExit(2)
print(f"Reading {', '.join(groups)} ...")
try:
    found = read_group_emails(groups, google_session())
except GroupReadError as exc:
    print(f"FAILED: {exc}", file=sys.stderr)
    raise SystemExit(1) from exc
print(f"{len(found)} people would be admitted:")
for email in sorted(found):
    print(f"  {email}")
