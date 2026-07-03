"""Regression tests for the identity-map/populate_existing issue in
`ensure_participant_group_and_commit`.

The concurrency invariant this file protects: two concurrent `start_session`
calls on a group-less experiment must serialize on the row lock and both
return the same group ID (the winner's). Without `populate_existing=True` on
the SELECT ... FOR UPDATE, SQLAlchemy returns the identity-mapped instance
with its stale cached `prolific_participant_group_id=None`, the "did the
other side win?" check silently misses the winner's write, and the second
caller overwrites the DB with its own group ID — stranding the winner's
raters in an orphaned group.
"""

from __future__ import annotations

import asyncio
import json
from uuid import uuid4

import respx
from httpx import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import get_settings
from models import Experiment
from services.participant_groups import ensure_participant_group_and_commit

PROLIFIC_BASE = "https://api.prolific.com/api/v1"


def _make_session_maker():
    settings = get_settings()
    engine = create_async_engine(settings.async_database_url)
    return engine, async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


def test_race_second_caller_returns_winner_not_own_new_group():
    """If session A commits a group ID first, session B (whose identity map
    still holds the stale `None`) must return A's group ID from
    `ensure_participant_group_and_commit`, not overwrite the DB with its own
    freshly-created one.

    This is the exact scenario `populate_existing=True` on the SELECT ...
    FOR UPDATE guards against — removing it makes this test fail (verified
    empirically before adding the fix).
    """
    # Save/restore around the settings singleton — `get_settings()` is
    # `@lru_cache`d and shared across the whole pytest session, so any leaked
    # mutation would flip `prolific.enabled` globally for subsequent tests.
    # Matches the `enable_prolific` fixture pattern.
    settings = get_settings()
    original_token = settings.prolific.api_token
    original_project_id = settings.prolific.project_id
    settings.prolific.api_token = "test-token"
    settings.prolific.project_id = "test-project"

    async def _run() -> None:
        engine, Session = _make_session_maker()
        try:
            # Set up an experiment with no group.
            async with Session() as setup:
                exp = Experiment(
                    name=f"race-{uuid4().hex[:8]}",
                    num_ratings_per_question=1,
                )
                setup.add(exp)
                await setup.commit()
                await setup.refresh(exp)
                exp_id = exp.id

            # Mock Prolific to return a distinct group ID per call so we can
            # tell which side "created" which group.
            call_counter = {"n": 0}

            def _prolific_responder(request):
                call_counter["n"] += 1
                body = json.loads(request.content.decode())
                return Response(
                    200,
                    json={
                        "id": f"G_call_{call_counter['n']}",
                        "name": body["name"],
                        "project_id": "test-project",
                    },
                )

            with respx.mock:
                respx.post(f"{PROLIFIC_BASE}/participant-groups/").mock(
                    side_effect=_prolific_responder
                )

                async with Session() as sess_a, Session() as sess_b:
                    # Both sessions load the experiment before either writes,
                    # putting Experiment(id=exp_id, group_id=None) in each
                    # session's identity map.
                    a_exp = (
                        await sess_a.execute(select(Experiment).where(Experiment.id == exp_id))
                    ).scalar_one()
                    b_exp = (
                        await sess_b.execute(select(Experiment).where(Experiment.id == exp_id))
                    ).scalar_one()
                    assert a_exp.prolific_participant_group_id is None
                    assert b_exp.prolific_participant_group_id is None

                    # A goes first and wins.
                    a_id = await ensure_participant_group_and_commit(a_exp, sess_a)
                    assert a_id == "G_call_1", f"Expected A to create G_call_1, got {a_id!r}"

                    # B's cached instance still has group_id=None. Without
                    # populate_existing, B's SELECT ... FOR UPDATE would return
                    # the identity-mapped instance unchanged, the check would
                    # miss A's write, and B would overwrite the DB with G_call_2.
                    b_id = await ensure_participant_group_and_commit(b_exp, sess_b)
                    assert b_id == "G_call_1", (
                        "Race lost: B should have returned A's group ID "
                        f"(G_call_1), but got {b_id!r}. Likely cause: "
                        "populate_existing=True missing on the SELECT ... "
                        "FOR UPDATE in ensure_participant_group_and_commit."
                    )

                # Sanity check: the DB row still holds A's ID.
                async with Session() as verify:
                    row = (
                        await verify.execute(
                            select(Experiment.prolific_participant_group_id).where(
                                Experiment.id == exp_id
                            )
                        )
                    ).scalar_one()
                    assert row == "G_call_1", f"DB overwritten: expected G_call_1, got {row!r}"
        finally:
            await engine.dispose()

    try:
        asyncio.run(_run())
    finally:
        settings.prolific.api_token = original_token
        settings.prolific.project_id = original_project_id
