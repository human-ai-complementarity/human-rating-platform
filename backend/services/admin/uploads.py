from __future__ import annotations

import csv
import io
import json
import logging
import sys
from typing import Any

import pyarrow.parquet as pq
from fastapi import HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Experiment, Question, Upload
from .mappers import build_upload_response
from .queries import fetch_experiment_or_404
from .status import assert_editable
from .validators import validate_csv_required_fields, validate_upload_filename

logger = logging.getLogger(__name__)

MAX_FILE_SIZE = 200 * 1024 * 1024  # 200MB

# The five fields a dataset can declare as file-level metadata. Keys are matched
# against this allowlist so unknown keys surface as a clean 400 instead of
# silently filling columns we don't model. Applies to both the CSV `#META:`
# header line and the Parquet schema's `dataset_meta` key — the colab notebook
# produces the same JSON shape for both.
DATASET_META_FIELDS = (
    "description",
    "system_prompt",
    "human_prompt_prefix",
    "human_prompt_suffix",
    "prolific_pool",
)
_META_PREFIX = "#META:"
_PARQUET_META_KEY = b"dataset_meta"
_REQUIRED_ROW_FIELDS = ("question_id", "question_text")


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


def _validate_meta_dict(parsed: Any) -> dict[str, str]:
    """Validate a decoded `#META:` / `dataset_meta` payload and normalise to str.

    Shared between the CSV and Parquet readers so the wire format and error
    messages stay identical. Raises HTTPException(400) for bad shapes — silently
    accepting them would let researcher mistakes (typos, wrong format) leak
    through unflagged.
    """
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=400,
            detail="dataset metadata must be a JSON object",
        )
    unknown = sorted(set(parsed) - set(DATASET_META_FIELDS))
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown dataset metadata keys: {', '.join(unknown)}. "
                f"Allowed keys: {', '.join(DATASET_META_FIELDS)}."
            ),
        )
    # Coerce all values to strings — JSON may have given us ints/bools for
    # `prolific_pool` etc. Drop empty strings so they don't overwrite existing values.
    return {k: str(v) for k, v in parsed.items() if v is not None and str(v) != ""}


def _parse_meta_header(text_stream: io.TextIOWrapper) -> dict[str, str] | None:
    """Peek the first line; if it starts with `#META:`, parse and consume it.

    Returns a dict on success, None when no meta header is present.
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
    return _validate_meta_dict(parsed)


def _parse_parquet_schema_meta(table: pq.lib.Table) -> dict[str, str] | None:
    """Extract dataset metadata from the Parquet schema's key-value metadata.

    Mirrors the CSV `#META:` line: looks for a JSON object under the
    `dataset_meta` key (the same key the colab notebook writes). Returns None
    when no key is present.
    """
    raw_metadata = table.schema.metadata or {}
    encoded = raw_metadata.get(_PARQUET_META_KEY)
    if encoded is None:
        return None
    try:
        parsed = json.loads(encoded.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid dataset_meta JSON in Parquet schema: {exc}",
        ) from exc
    return _validate_meta_dict(parsed)


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


def _serialize_cell(value: Any) -> str:
    """Coerce a Parquet cell value into the canonical DB string form.

    Lists become pipe-joined strings (the canonical `options` format the frontend
    already parses first); dicts and structs become JSON (matching what CSV
    uploads store in `metadata`). Scalars fall through to `str()`. None becomes
    "" to match CSV's empty-cell behaviour.

    Without this, `str([...])` would produce a Python list repr ("['A', 'B']")
    and `str({...})` would produce a non-JSON dict repr ("{'k': 1}") — the
    downstream code expects pipe-separated and JSON respectively.
    """
    if value is None:
        return ""
    if isinstance(value, list):
        return "|".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value)
    return str(value)


def _read_csv(file: UploadFile) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
    """Parse a CSV upload into (rows, meta).

    Rows are returned as raw dict[str, str] — values are whatever the CSV
    reader produced. `meta` is the parsed `#META:` line or None.
    """
    _configure_csv_field_limit()
    # `utf-8-sig` consumes a leading BOM if present (Excel "Save As CSV UTF-8"
    # and `pandas.to_csv(encoding="utf-8-sig")` both add one). Without this, the
    # BOM ends up on the first line and the `#META:` check fails silently.
    text_stream = io.TextIOWrapper(file.file, encoding="utf-8-sig", newline="")
    try:
        meta = _parse_meta_header(text_stream)
        reader = csv.DictReader(text_stream)
        rows: list[dict[str, Any]] = []
        for row in reader:
            validate_csv_required_fields(row, _REQUIRED_ROW_FIELDS)
            rows.append(row)
        return rows, meta
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded") from exc
    except csv.Error as exc:
        raise HTTPException(status_code=400, detail=f"Invalid CSV file: {exc}") from exc
    finally:
        try:
            text_stream.detach()
        except Exception:
            pass


def _read_parquet(
    file: UploadFile,
) -> tuple[list[dict[str, Any]], dict[str, str] | None]:
    """Parse a Parquet upload into (rows, meta).

    Each row dict has the same shape the CSV reader produces — `options` becomes
    a `|`-joined string, `metadata` becomes a JSON string, scalars become their
    string repr. This keeps the downstream Question writer format-agnostic.
    """
    try:
        table = pq.read_table(file.file)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid Parquet file: {exc}") from exc

    column_names = set(table.column_names)
    for required in _REQUIRED_ROW_FIELDS:
        if required not in column_names:
            raise HTTPException(
                status_code=400,
                detail=f"Missing required column: {required}",
            )

    meta = _parse_parquet_schema_meta(table)
    rows = [
        {key: _serialize_cell(value) for key, value in row.items()} for row in table.to_pylist()
    ]
    return rows, meta


def _build_questions_with_parent_refs(
    experiment_id: int,
    rows: list[dict[str, Any]],
) -> tuple[list[Question], list[str]]:
    """Construct (un-flushed) Question rows and collect raw parent_question_id refs.

    Parent refs are returned alongside so the caller can resolve them once the
    new rows have DB ids.
    """
    new_questions: list[Question] = []
    parent_refs: list[str] = []
    for row in rows:
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
        parent_refs.append((row.get("parent_question_id") or "").strip())
    return new_questions, parent_refs


async def _resolve_parent_refs(
    experiment_id: int,
    new_questions: list[Question],
    parent_refs: list[str],
    db: AsyncSession,
) -> None:
    """Set Question.parent_question_id for each row whose CSV ref points at a sibling.

    Caller must have flushed `new_questions` so they have DB ids. Raises
    HTTPException(400) for self-references or unresolvable parent strings.
    """
    if not any(parent_refs):
        return

    # Build {question_id_string -> db id} for this experiment, covering both rows
    # just inserted and any pre-existing ones from earlier uploads.
    existing = (
        await db.execute(
            select(Question.question_id, Question.id).where(Question.experiment_id == experiment_id)
        )
    ).all()
    question_id_to_db_id: dict[str, int] = {}
    for qid_string, db_id in existing:
        # Last write wins on duplicate question_id strings — questions already
        # allow duplicates within an experiment, and the CSV-string parent ref
        # is inherently ambiguous in that case. We pick whichever the DB returns.
        question_id_to_db_id[qid_string] = db_id

    for question, parent_ref in zip(new_questions, parent_refs):
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


async def upload_questions(
    experiment_id: int,
    file: UploadFile,
    db: AsyncSession,
) -> dict[str, Any]:
    """Ingest a CSV or Parquet upload into the experiment.

    File-level dataset metadata is read from the format's natural location —
    `#META:` for CSV, schema key-value for Parquet — and applied with first-
    upload-wins semantics; conflicting values from later uploads are surfaced
    in the response but never overwrite saved fields.
    """
    experiment = await fetch_experiment_or_404(experiment_id, db)
    assert_editable(experiment, action="upload questions")
    extension = validate_upload_filename(file)

    if _get_upload_size(file) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File size exceeds 200MB limit")

    await file.seek(0)
    if extension == "csv":
        rows, meta = _read_csv(file)
    else:
        rows, meta = _read_parquet(file)

    meta_applied: list[str] = []
    meta_conflicts: list[str] = []
    if meta:
        meta_applied, meta_conflicts = _apply_meta_to_experiment(experiment, meta)

    new_questions, parent_refs = _build_questions_with_parent_refs(experiment_id, rows)
    for question in new_questions:
        db.add(question)

    # Flush so newly inserted rows have DB ids before we resolve parent references.
    await db.flush()
    await _resolve_parent_refs(experiment_id, new_questions, parent_refs, db)

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
                "format": extension,
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
