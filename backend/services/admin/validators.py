from __future__ import annotations

from typing import Any, Iterable

from fastapi import HTTPException, UploadFile

_ACCEPTED_UPLOAD_EXTENSIONS = ("csv", "parquet")


def validate_upload_filename(file: UploadFile) -> str:
    """Reject uploads with an unsupported extension; return the matched extension.

    Returns the lowercase extension without the leading dot (e.g. `"csv"` or
    `"parquet"`) so the caller can dispatch on it without re-parsing.
    """
    name = (file.filename or "").lower()
    for ext in _ACCEPTED_UPLOAD_EXTENSIONS:
        if name.endswith(f".{ext}"):
            return ext
    raise HTTPException(
        status_code=400,
        detail=f"File must be one of: {', '.join(_ACCEPTED_UPLOAD_EXTENSIONS)}",
    )


def validate_csv_required_fields(row: dict[str, Any], required_fields: Iterable[str]) -> None:
    for field in required_fields:
        if field not in row:
            raise HTTPException(status_code=400, detail=f"Missing required field: {field}")
