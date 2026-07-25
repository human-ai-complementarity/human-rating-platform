"""Seed one experiment per admin-list notification ("action needed" dot) state.

Populates the DB directly (no Prolific calls) with a demo experiment for every
branch of services.admin.status.compute_attention_reason, plus the states that
deliberately carry no dot. Idempotent: re-running first deletes any prior demo
rows (matched by the DEMO_PREFIX on the public name; FK ON DELETE CASCADE clears
the child questions/rounds/raters/ratings).

Run inside the api/migrate container:
    uv run --no-sync python scripts/seed_notification_states.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete
from sqlmodel import Session, create_engine, select

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from config import get_settings  # noqa: E402
from models import (  # noqa: E402
    Experiment,
    ExperimentRound,
    ExperimentStatus,
    ProlificStudyStatus,
    Question,
    Rater,
    Rating,
)

DEMO_PREFIX = "[demo] "
TARGET = 3  # num_ratings_per_question for every demo experiment
NUM_QUESTIONS = 4

# Each entry: the row label, the experiment status, the rounds to attach
# (round_number, prolific_study_status, total_cost minor units), how many
# raters have rated every question so far, and the dot we expect the list
# endpoint to show. `total_cost` mimics Prolific's per-study cost so the demo
# shows realistic per-experiment and total spend. `expected` is documentation
# only — it is not written to the DB.
DEMO_STATES = [
    {
        "label": "Draft — setup in progress",
        "status": ExperimentStatus.DRAFT,
        "rounds": [],
        "ratings_per_question": 0,
        "expected": "NO dot (a plain draft is not actionable)",
    },
    {
        "label": "Draft — unpublished pilot draft",
        "status": ExperimentStatus.DRAFT,
        "rounds": [(0, ProlificStudyStatus.UNPUBLISHED, 0)],
        "ratings_per_question": 0,
        "expected": "DOT — publish the round draft",
    },
    {
        "label": "Launched — unpublished round draft",
        "status": ExperimentStatus.LAUNCH,
        "rounds": [
            (0, ProlificStudyStatus.COMPLETED, 620),
            (1, ProlificStudyStatus.UNPUBLISHED, 0),
        ],
        "ratings_per_question": 1,
        "expected": "DOT — publish the round draft",
    },
    {
        "label": "Launched — round actively collecting",
        "status": ExperimentStatus.LAUNCH,
        "rounds": [(1, ProlificStudyStatus.ACTIVE, 620)],
        "ratings_per_question": 1,
        "expected": "NO dot (a round is still collecting — just wait)",
    },
    {
        "label": "Launched — rounds closed, below target",
        "status": ExperimentStatus.LAUNCH,
        "rounds": [
            (0, ProlificStudyStatus.COMPLETED, 240),
            (1, ProlificStudyStatus.AWAITING_REVIEW, 240),
        ],
        "ratings_per_question": 1,
        "expected": "DOT — launch another round",
    },
    {
        "label": "Launched — target met, ready to finish",
        "status": ExperimentStatus.LAUNCH,
        "rounds": [(1, ProlificStudyStatus.COMPLETED, 1860)],
        "ratings_per_question": TARGET,
        "expected": "DOT — mark the experiment finished",
    },
    {
        "label": "Finished",
        "status": ExperimentStatus.FINISHED,
        "rounds": [(1, ProlificStudyStatus.COMPLETED, 1860)],
        "ratings_per_question": TARGET,
        "expected": "NO dot (terminal)",
    },
]


def _seed_experiment(session: Session, spec: dict, *, order: int, now: datetime) -> Experiment:
    # Stagger created_at so the list (ordered created_at DESC) shows the demos
    # top-to-bottom in DEMO_STATES order.
    created_at = now - timedelta(minutes=order)
    experiment = Experiment(
        name=f"{DEMO_PREFIX}{spec['label']}",
        internal_name=spec["label"],
        num_ratings_per_question=TARGET,
        prolific_completion_url="https://app.prolific.com/submissions/complete?cc=DEMO",
        status=spec["status"],
        created_at=created_at,
    )
    session.add(experiment)
    session.flush()  # assign experiment.id

    questions: list[Question] = []
    for q in range(1, NUM_QUESTIONS + 1):
        question = Question(
            experiment_id=experiment.id,
            question_id=f"demo-{experiment.id}-{q}",
            question_text=f"Demo question {q} for {spec['label']}",
            gt_answer="",
            options="Yes|No",
            question_type="MC",
            extra_data="{}",
        )
        session.add(question)
        questions.append(question)
    session.flush()  # assign question ids

    for round_number, study_status, total_cost in spec["rounds"]:
        session.add(
            ExperimentRound(
                experiment_id=experiment.id,
                round_number=round_number,
                prolific_study_id=f"SEED_{experiment.id}_R{round_number}",
                prolific_study_status=study_status,
                description="Demo round (seeded, no real Prolific study).",
                estimated_completion_time=10,
                reward=100,
                device_compatibility='["desktop"]',
                places_requested=TARGET,
                total_cost=total_cost,
                created_at=created_at,
            )
        )

    # Each of `ratings_per_question` raters rates every question once, so every
    # question ends with exactly that many ratings — controls remaining vs target.
    for r in range(spec["ratings_per_question"]):
        rater = Rater(
            prolific_id=f"DEMO_PID_{experiment.id}_{r}",
            study_id=f"DEMO_STUDY_{experiment.id}",
            session_id=f"DEMO_SESSION_{experiment.id}_{r}",
            experiment_id=experiment.id,
            session_start=created_at,
            session_end=created_at + timedelta(minutes=5),
            is_active=False,
        )
        session.add(rater)
        session.flush()  # assign rater.id
        for question in questions:
            session.add(
                Rating(
                    question_id=question.id,
                    rater_id=rater.id,
                    answer="Yes",
                    confidence=4,
                    time_started=created_at,
                    time_submitted=created_at + timedelta(seconds=30),
                )
            )

    return experiment


def main() -> int:
    settings = get_settings()
    engine = create_engine(settings.sync_database_url, pool_pre_ping=True)

    with Session(engine) as session:
        demo_ids = session.exec(
            select(Experiment.id).where(Experiment.name.like(f"{DEMO_PREFIX}%"))
        ).all()
        if demo_ids:
            # Delete children first (explicit, FK-safe regardless of ON DELETE rules).
            question_ids = session.exec(
                select(Question.id).where(Question.experiment_id.in_(demo_ids))
            ).all()
            rater_ids = session.exec(
                select(Rater.id).where(Rater.experiment_id.in_(demo_ids))
            ).all()
            if question_ids:
                session.execute(delete(Rating).where(Rating.question_id.in_(question_ids)))
            if rater_ids:
                session.execute(delete(Rating).where(Rating.rater_id.in_(rater_ids)))
            session.execute(delete(ExperimentRound).where(ExperimentRound.experiment_id.in_(demo_ids)))
            session.execute(delete(Rater).where(Rater.experiment_id.in_(demo_ids)))
            session.execute(delete(Question).where(Question.experiment_id.in_(demo_ids)))
            session.execute(delete(Experiment).where(Experiment.id.in_(demo_ids)))
            session.commit()
            print(f"Removed {len(demo_ids)} prior demo experiment(s).")

        now = datetime.now(UTC)
        for order, spec in enumerate(DEMO_STATES):
            experiment = _seed_experiment(session, spec, order=order, now=now)
            session.flush()
            print(f"  id={experiment.id:>3}  {spec['label']:<42}→ {spec['expected']}")
        session.commit()

    print(f"\nSeeded {len(DEMO_STATES)} demo experiments. Reload the admin list to view.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
