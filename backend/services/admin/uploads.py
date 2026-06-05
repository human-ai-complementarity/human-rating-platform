from __future__ import annotations

import csv
import io
import logging
import sys
from typing import Any

from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Question, Upload
from .mappers import build_upload_response
from .queries import fetch_experiment_or_404
from .validators import validate_csv_required_fields, validate_csv_upload

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 200 * 1024 * 1024  # 200MB


def _configure_csv_field_limit() -> None:
    """Raise Python's per-field CSV cap so long-context rows can be parsed."""
    limit = sys.maxsize
    while True:
        try:
            csv.field_size_limit(limit)
            return
        except OverflowError:
            limit //= 10


def _get_upload_size(file: UploadFile) -> int:
    """Measure the uploaded file without loading it fully into memory."""
    stream = file.file
    current = stream.tell()
    stream.seek(0, io.SEEK_END)
    size = stream.tell()
    stream.seek(current)
    return size


async def upload_questions_csv(
    experiment_id: int,
    file: UploadFile,
    db: AsyncSession,
) -> dict[str, str]:
    await fetch_experiment_or_404(experiment_id, db)
    validate_csv_upload(file)
    _configure_csv_field_limit()

    if _get_upload_size(file) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File size exceeds 200MB limit")

    await file.seek(0)
    text_stream = io.TextIOWrapper(file.file, encoding="utf-8", newline="")
    try:
        reader = csv.DictReader(text_stream)
        required_fields = ["question_id", "question_text"]
        rows: list[dict[str, Any]] = []
        new_questions: list[Question] = []

        for row in reader:
            validate_csv_required_fields(row, required_fields)
            rows.append(row)
            new_questions.append(
                Question(
                    experiment_id=experiment_id,
                    question_id=row["question_id"],
                    question_text=row["question_text"],
                    gt_answer=row.get("gt_answer") or "",
                    options=row.get("options") or "",
                    question_type=row.get("question_type") or "MC",
                    extra_data=row.get("metadata") or "{}",
                )
            )
            db.add(new_questions[-1])
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded") from exc
    except csv.Error as exc:
        raise HTTPException(status_code=400, detail=f"Invalid CSV file: {exc}") from exc
    finally:
        try:
            text_stream.detach()
        except Exception:
            pass

    # Flush so newly inserted rows have DB ids before we resolve parent references.
    await db.flush()

    parent_refs = {
        (row.get("parent_question_id") or "").strip()
        for row in rows
        if (row.get("parent_question_id") or "").strip()
    }
    if parent_refs:
        # Build {question_id_string -> db id} for this experiment, covering both rows
        # just inserted and any pre-existing ones from earlier uploads.
        existing = (
            await db.execute(
                select(Question.question_id, Question.id).where(
                    Question.experiment_id == experiment_id
                )
            )
        ).all()
        question_id_to_db_id: dict[str, int] = {}
        for qid_string, db_id in existing:
            # Last write wins on duplicate question_id strings — questions already
            # allow duplicates within an experiment, and the CSV-string parent ref
            # is inherently ambiguous in that case. We pick whichever the DB returns.
            question_id_to_db_id[qid_string] = db_id

        for question, row in zip(new_questions, rows):
            parent_ref = (row.get("parent_question_id") or "").strip()
            if not parent_ref:
                continue
            if parent_ref == question.question_id:
                raise HTTPException(
                    status_code=400,
                    detail=f"Question '{question.question_id}' cannot reference itself as parent",
                )
            parent_db_id = question_id_to_db_id.get(parent_ref)
            if parent_db_id is None:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"parent_question_id '{parent_ref}' (referenced by '{question.question_id}') "
                        f"does not match any question in this experiment"
                    ),
                )
            question.parent_question_id = parent_db_id

    questions_added = len(new_questions)
    db.add(
        Upload(
            experiment_id=experiment_id,
            filename=file.filename,
            question_count=questions_added,
        )
    )
    await db.commit()

    logger.info(
        "Question batch uploaded",
        extra={
            "attributes": {
                "experiment_id": experiment_id,
                "question_count": questions_added,
                "filename": file.filename,
            }
        },
    )

    return {"message": f"Uploaded {questions_added} questions"}


async def list_uploads(
    experiment_id: int,
    skip: int,
    limit: int,
    db: AsyncSession,
) -> list[dict[str, Any]]:
    await fetch_experiment_or_404(experiment_id, db)

    uploads = (
        (
            await db.execute(
                select(Upload)
                .where(Upload.experiment_id == experiment_id)
                .order_by(Upload.uploaded_at.desc())
                .offset(skip)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    return [build_upload_response(upload) for upload in uploads]
