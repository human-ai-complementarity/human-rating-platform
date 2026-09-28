"""One-time backfill: seed datasets and put past collections into their groups.

Only experiments in the vendored collection record (`COLLECTIONS` in
services/admin/dataset_catalog.py) are ever assigned, each to the wave the
pipeline recorded for it, after its card, arm and names are cross-checked.
Everything else is reported and left alone.

Dry run by default: prints exactly what would change, then rolls back. Pass
--apply to write. Read the dry run first. Assignment bypasses the post-launch
group lock, so a wrong assignment on a launched experiment can only be undone
in the database.

Run inside the api/migrate container:
    uv run --no-sync python scripts/sync_dataset_catalog.py            # dry run
    uv run --no-sync python scripts/sync_dataset_catalog.py --apply    # write

It connects with the app's normal database settings, so it also works from any
machine that can reach the database with those settings in the environment.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import get_settings  # noqa: E402
from services.admin.dataset_catalog import (  # noqa: E402
    DatasetCatalogSyncReport,
    sync_dataset_catalog,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the changes. Without this flag the run is a dry run.",
    )
    return parser


def format_report(report: DatasetCatalogSyncReport) -> str:
    """Human-readable summary; groups are named rather than numbered.

    In a dry run, ids of newly created groups belong to rolled-back rows.
    """
    mode = (
        "APPLIED" if report.applied else "DRY RUN (nothing written; re-run with --apply to write)"
    )
    lines = [f"Dataset catalog sync: {mode}", f"Pipeline snapshot: {report.pipeline_commit}", ""]

    def names(title: str, items: list[str]) -> None:
        lines.append(f"{title} ({len(items)}){': ' + ', '.join(items) if items else ''}")

    names("Datasets created", report.datasets_created)
    names("Datasets updated", report.datasets_updated)
    names("Groups created", report.groups_created)
    names("Collected in a wave the card does not schedule", report.collected_outside_schedule)

    # Internal names first: public names repeat across arms of one dataset.
    lines.append(f"Experiments assigned ({len(report.experiments_assigned)})")
    for item in report.experiments_assigned:
        label = item.internal_name or item.experiment_name
        lines.append(
            f'  #{item.experiment_id} "{label}" -> {item.dataset_name} {item.wave}, '
            f'arm {item.arm} (group "{item.group_name}")'
        )
    lines.append(f"Experiments skipped ({len(report.experiments_skipped)})")
    for item in report.experiments_skipped:
        label = item.internal_name or item.experiment_name
        detail = f" ({item.detail})" if item.detail else ""
        lines.append(f'  #{item.experiment_id} "{label}": {item.reason}{detail}')
    if report.manifest_missing:
        missing = ", ".join(str(eid) for eid in report.manifest_missing)
        lines.append(f"Listed but not in this database ({len(report.manifest_missing)}): {missing}")
    return "\n".join(lines)


async def _run(apply: bool) -> DatasetCatalogSyncReport:
    engine = create_async_engine(get_settings().async_database_url, pool_pre_ping=True)
    try:
        # Same session settings as the app (database.py), so a dry run behaves
        # exactly like the write it previews.
        session_maker = async_sessionmaker(
            engine,
            class_=AsyncSession,
            autoflush=False,
            expire_on_commit=False,
        )
        async with session_maker() as db:
            return await sync_dataset_catalog(db, apply=apply)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = asyncio.run(_run(args.apply))
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
