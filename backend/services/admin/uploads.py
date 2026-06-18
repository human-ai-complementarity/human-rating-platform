from __future__ import annotations

import csv
import io
import json
import logging
import sys
from typing import Any

from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Experiment, Question, Upload
from .mappers import build_upload_response
from .queries import fetch_experiment_or_404
from .validators import validate_csv_required_fields, validate_csv_upload

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 200 * 1024 * 1024  # 200MB

# The four fields a dataset CSV can declare on its `#META:` header line. Keys
# are matched against this allowlist so unknown keys surface as a clean 400 instead
# of silently filling columns we don't model.
DATASET_META_FIELDS = (
    "description",
    "system_prompt",
    "human_prompt_prefix",
    "human_prompt_suffix",
    "prolific_pool",
)
_META_PREFIX = "#META:"


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


def _parse_meta_header(text_stream: io.TextIOWrapper) -> dict[str, str] | None:
    """Peek the first line; if it starts with `#META:`, parse and consume it.

    Returns a dict on success, None when no meta header is present. Raises
    HTTPException(400) if the header is present but the JSON is invalid or
    contains keys outside DATASET_META_FIELDS — silently accepting either
    would let researcher mistakes (typos, wrong format) leak through unflagged.
    """
    first_line = text_stream.readline()
    if not first_line.lstrip().startswith(_META_PREFIX):
        # Not a meta header — rewind so csv.DictReader sees the column header row.
        text_stream.seek(0)
        return None

    json_text = first_line.lstrip()[len(_META_PREFIX) :].strip()
    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid #META: JSON on first line of CSV: {exc.msg}",
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=400,
            detail="#META: must be a JSON object",
        )

    unknown = sorted(set(parsed) - set(DATASET_META_FIELDS))
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown #META: keys: {', '.join(unknown)}. "
                f"Allowed keys: {', '.join(DATASET_META_FIELDS)}."
            ),
        )

    # Coerce all values to strings — JSON may have given us ints/bools for
    # `prolific_pool` etc. Drop empty strings so they don't overwrite existing values.
    return {k: str(v) for k, v in parsed.items() if v is not None and str(v) != ""}


def _apply_meta_to_experiment(
    experiment: Experiment, meta: dict[str, str]
) -> tuple[list[str], list[str]]:
    """Apply meta to experiment fields without overwriting existing non-empty values.

    Returns `(applied, conflicts)`. `applied` lists keys actually written to a
    blank field on the experiment. `conflicts` lists keys whose declared value
    disagrees with an already-populated field — those are never overwritten.
    A key whose declared value equals the existing value is in neither list.
    """
    applied: list[str] = []
    conflicts: list[str] = []
    for field_name in DATASET_META_FIELDS:
        if field_name not in meta:
            continue
        new_value = meta[field_name]
        current_value = getattr(experiment, field_name) or ""
        if not current_value:
            setattr(experiment, field_name, new_value)
            applied.append(field_name)
        elif current_value != new_value:
            conflicts.append(field_name)
    return applied, conflicts


async def upload_questions_csv(
    experiment_id: int,
    file: UploadFile,
    db: AsyncSession,
) -> dict[str, Any]:
    experiment = await fetch_experiment_or_404(experiment_id, db)
    validate_csv_upload(file)
    _configure_csv_field_limit()

    if _get_upload_size(file) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File size exceeds 200MB limit")

    await file.seek(0)
    # `utf-8-sig` consumes a leading BOM if present (Excel "Save As CSV UTF-8"
    # and `pandas.to_csv(encoding="utf-8-sig")` both add one). Without this, the
    # BOM ends up on the first line and the `#META:` check fails silently.
    text_stream = io.TextIOWrapper(file.file, encoding="utf-8-sig", newline="")
    meta: dict[str, str] | None = None
    meta_applied: list[str] = []
    meta_conflicts: list[str] = []
    try:
        meta = _parse_meta_header(text_stream)
        if meta:
            meta_applied, meta_conflicts = _apply_meta_to_experiment(experiment, meta)
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
            dataset_meta=json.dumps(meta) if meta else None,
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
                "meta_keys": sorted(meta.keys()) if meta else [],
                "meta_conflicts": meta_conflicts,
            }
        },
    )

    return {
        "message": f"Uploaded {questions_added} questions",
        "meta_applied": sorted(meta_applied),
        "meta_conflicts": meta_conflicts,
    }


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
