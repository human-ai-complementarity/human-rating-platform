from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, UploadFile, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from database import get_session
from models import ExperimentStatus
from schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyResponse,
    DatasetCreate,
    DatasetResponse,
    DatasetUpdate,
    ExperimentRoundCreate,
    ExperimentRoundResponse,
    ExperimentRoundUpdate,
    ExperimentCreate,
    ExperimentResponse,
    ExperimentUpdate,
    PilotStudyCreate,
    PlatformStatus,
    RecommendationResponse,
)
from models import ApiKey
from services import admin as admin_service
from services import api_keys as api_key_service
from services.admin.prolific import get_cached_workspace_currency
from auth import AdminSession, require_admin, get_admin_manager
from services.authn import verify_clerk_token_and_get_email

# Public admin router (for auth endpoints)
router = APIRouter(prefix="/admin", tags=["admin"])

# Secure router for admin-only endpoints
secure_router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_admin)])


async def get_clerk_email_from_request(request: Request) -> str:
    # Require a Clerk session token via Authorization: Bearer <token>
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    token = auth.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Invalid Bearer token")

    settings = get_settings()
    try:
        email = await verify_clerk_token_and_get_email(token, settings)
    except HTTPException:
        # Pass through explicit HTTP errors (e.g., 401)
        raise
    except Exception:
        # Hide internals behind a generic 401
        raise HTTPException(status_code=401, detail="Invalid Clerk token")

    return email


@router.post("/auth/login")
async def admin_login(
    email: str = Depends(get_clerk_email_from_request),
    manager=Depends(get_admin_manager),
):
    settings = get_settings()
    allow = {e.strip().lower() for e in settings.admin_allowlist}
    if email.strip().lower() not in allow:
        return JSONResponse(status_code=403, content={"message": "Email is not allowlisted"})

    resp = JSONResponse({"ok": True})
    manager.set_cookie(resp, email.strip())
    return resp


@router.post("/auth/logout")
async def admin_logout(manager=Depends(get_admin_manager)):
    resp = JSONResponse({"ok": True})
    manager.clear_cookie(resp)
    return resp


@router.get("/platform-status", response_model=PlatformStatus)
async def get_platform_status():
    settings = get_settings()
    code, symbol = await get_cached_workspace_currency(settings.prolific)
    return PlatformStatus(
        prolific_enabled=settings.prolific.enabled,
        currency_code=code,
        currency_symbol=symbol,
    )


@secure_router.post("/experiments", response_model=ExperimentResponse)
async def create_experiment(
    experiment: ExperimentCreate,
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.create_experiment(experiment, db)


@secure_router.get("/experiments", response_model=list[ExperimentResponse])
async def list_experiments(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    archived: bool = Query(False),
    include_archived: bool = Query(False),
    status: ExperimentStatus | None = Query(None),
    search: str | None = Query(None, max_length=255),
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.list_experiments(
        skip=skip,
        limit=limit,
        archived=archived,
        include_archived=include_archived,
        status=status,
        search=search,
        db=db,
    )


@secure_router.get("/experiments/{experiment_id}", response_model=ExperimentResponse)
async def get_experiment(
    experiment_id: int,
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.get_experiment(experiment_id=experiment_id, db=db)


@secure_router.post("/experiments/{experiment_id}/upload")
async def upload_questions(
    experiment_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.upload_questions(
        experiment_id=experiment_id,
        file=file,
        db=db,
    )


@secure_router.get("/experiments/{experiment_id}/uploads")
async def list_uploads(
    experiment_id: int,
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.list_uploads(
        experiment_id=experiment_id,
        skip=skip,
        limit=limit,
        db=db,
    )


@secure_router.get("/experiments/{experiment_id}/export")
async def export_ratings(
    experiment_id: int,
    include_preview: bool = Query(False),
    db: AsyncSession = Depends(get_session),
):
    return StreamingResponse(
        admin_service.stream_export_csv_chunks(
            experiment_id=experiment_id, db=db, include_preview=include_preview
        ),
        media_type="text/csv",
        headers={
            "Content-Disposition": (
                f"attachment; filename={admin_service.build_export_filename(experiment_id)}"
            )
        },
    )


@secure_router.patch("/experiments/{experiment_id}", response_model=ExperimentResponse)
async def update_experiment(
    experiment_id: int,
    payload: ExperimentUpdate,
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.update_experiment(
        experiment_id=experiment_id, payload=payload, db=db
    )


@secure_router.delete("/experiments/{experiment_id}")
async def delete_experiment(
    experiment_id: int,
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.delete_experiment(experiment_id=experiment_id, db=db)


@secure_router.post("/experiments/{experiment_id}/duplicate", response_model=ExperimentResponse)
async def duplicate_experiment(
    experiment_id: int,
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.duplicate_experiment(experiment_id=experiment_id, db=db)


@secure_router.post("/experiments/{experiment_id}/finish", response_model=ExperimentResponse)
async def finish_experiment(
    experiment_id: int,
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.finish_experiment(experiment_id=experiment_id, db=db)


@secure_router.post("/experiments/{experiment_id}/archive", response_model=ExperimentResponse)
async def archive_experiment(
    experiment_id: int,
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.archive_experiment(experiment_id=experiment_id, db=db)


@secure_router.post("/experiments/{experiment_id}/unarchive", response_model=ExperimentResponse)
async def unarchive_experiment(
    experiment_id: int,
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.unarchive_experiment(experiment_id=experiment_id, db=db)


@secure_router.get("/experiments/{experiment_id}/stats")
async def get_experiment_stats(
    experiment_id: int,
    include_preview: bool = Query(False),
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.get_experiment_stats(
        experiment_id=experiment_id, db=db, include_preview=include_preview
    )


@secure_router.get("/experiments/{experiment_id}/analytics")
async def get_experiment_analytics(
    experiment_id: int,
    include_preview: bool = Query(False),
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.get_experiment_analytics(
        experiment_id=experiment_id, db=db, include_preview=include_preview
    )


@secure_router.post(
    "/experiments/{experiment_id}/prolific/pilot",
    response_model=ExperimentRoundResponse,
)
async def run_pilot_study(
    experiment_id: int,
    payload: PilotStudyCreate,
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.run_pilot_study(experiment_id=experiment_id, payload=payload, db=db)


@secure_router.get(
    "/experiments/{experiment_id}/prolific/recommend", response_model=RecommendationResponse
)
async def get_prolific_recommendation(
    experiment_id: int,
    include_preview: bool = Query(False),
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.calculate_recommendation(
        experiment_id=experiment_id,
        db=db,
        include_preview=include_preview,
    )


@secure_router.post(
    "/experiments/{experiment_id}/prolific/rounds",
    response_model=ExperimentRoundResponse,
)
async def run_experiment_round(
    experiment_id: int,
    payload: ExperimentRoundCreate,
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.run_experiment_round(
        experiment_id=experiment_id, payload=payload, db=db
    )


@secure_router.get(
    "/experiments/{experiment_id}/prolific/rounds",
    response_model=list[ExperimentRoundResponse],
)
async def list_experiment_rounds(
    experiment_id: int,
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.list_experiment_rounds(experiment_id=experiment_id, db=db)


@secure_router.post("/experiments/{experiment_id}/prolific/sync-spend")
async def sync_experiment_spend(
    experiment_id: int,
    db: AsyncSession = Depends(get_session),
):
    """Refresh every round's Prolific cost and return the experiment's total
    spend (minor units). Called from the detail view to hydrate the spend card."""
    spend = await admin_service.refresh_experiment_spend(experiment_id=experiment_id, db=db)
    return {"spend_minor_units": spend}


@secure_router.patch(
    "/experiments/{experiment_id}/prolific/rounds/{round_id}",
    response_model=ExperimentRoundResponse,
)
async def update_experiment_round(
    experiment_id: int,
    round_id: int,
    payload: ExperimentRoundUpdate,
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.update_experiment_round(
        experiment_id=experiment_id,
        round_id=round_id,
        payload=payload,
        db=db,
    )


@secure_router.post("/experiments/{experiment_id}/prolific/rounds/{round_id}/publish")
async def publish_experiment_round(
    experiment_id: int,
    round_id: int,
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.publish_experiment_round(
        experiment_id=experiment_id,
        round_id=round_id,
        db=db,
    )


@secure_router.post("/experiments/{experiment_id}/prolific/rounds/{round_id}/close")
async def close_experiment_round(
    experiment_id: int,
    round_id: int,
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.close_experiment_round(
        experiment_id=experiment_id,
        round_id=round_id,
        db=db,
    )


@secure_router.delete("/experiments/{experiment_id}/prolific/rounds/{round_id}")
async def discard_experiment_round(
    experiment_id: int,
    round_id: int,
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.discard_experiment_round(
        experiment_id=experiment_id,
        round_id=round_id,
        db=db,
    )


# ── Datasets (identity anchor for experiment grouping) ──────────────────────


@secure_router.get("/datasets", response_model=list[DatasetResponse])
async def list_datasets(db: AsyncSession = Depends(get_session)):
    """List all datasets, ordered by name (case-insensitive)."""
    return await admin_service.list_datasets(db)


@secure_router.post("/datasets", response_model=DatasetResponse)
async def create_dataset(
    payload: DatasetCreate,
    db: AsyncSession = Depends(get_session),
):
    """Register a dataset.

    `name` is unique case-insensitively (409 on duplicate). For datasets from
    the inference pipeline, use the card name verbatim — it is the cross-repo
    join key. `waves` is the set of wave tokens the dataset is included in
    (e.g. `["fall25", "sp26"]`), lowercased and deduped on write.
    """
    return await admin_service.create_dataset(payload, db)


@secure_router.get("/datasets/{dataset_id}", response_model=DatasetResponse)
async def get_dataset(
    dataset_id: int,
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.get_dataset(dataset_id, db)


@secure_router.patch("/datasets/{dataset_id}", response_model=DatasetResponse)
async def update_dataset(
    dataset_id: int,
    payload: DatasetUpdate,
    db: AsyncSession = Depends(get_session),
):
    """Partially update a dataset. Omitted fields are left unchanged;
    `waves` replaces the whole set when sent."""
    return await admin_service.update_dataset(dataset_id, payload, db)


@secure_router.delete("/datasets/{dataset_id}")
async def delete_dataset(
    dataset_id: int,
    db: AsyncSession = Depends(get_session),
):
    await admin_service.delete_dataset(dataset_id, db)
    return {"ok": True}


# ── API keys (bearer credentials for the /api/v1 programmatic API) ──────────


def _api_key_response(record: ApiKey) -> ApiKeyResponse:
    return ApiKeyResponse(
        id=record.id,
        name=record.name,
        masked_key=api_key_service.mask(record),
        created_at=record.created_at,
        last_used_at=record.last_used_at,
        revoked_at=record.revoked_at,
        created_by=record.created_by,
        is_active=record.revoked_at is None,
    )


def _api_key_created(issued: api_key_service.IssuedApiKey) -> ApiKeyCreated:
    base = _api_key_response(issued.record)
    return ApiKeyCreated(**base.model_dump(), plaintext_key=issued.plaintext)


async def _get_api_key_or_404(key_id: int, db: AsyncSession) -> ApiKey:
    record = await api_key_service.get_api_key_or_none(key_id, db)
    if record is None:
        raise HTTPException(status_code=404, detail="API key not found")
    return record


@secure_router.get("/api-keys", response_model=list[ApiKeyResponse])
async def list_api_keys(db: AsyncSession = Depends(get_session)):
    records = await api_key_service.list_api_keys(db)
    return [_api_key_response(record) for record in records]


@secure_router.post("/api-keys", response_model=ApiKeyCreated)
async def create_api_key(
    payload: ApiKeyCreate,
    admin: AdminSession = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
):
    issued = await api_key_service.create_api_key(payload.name, db, created_by=admin.email)
    return _api_key_created(issued)


@secure_router.post("/api-keys/{key_id}/regenerate", response_model=ApiKeyCreated)
async def regenerate_api_key(
    key_id: int,
    db: AsyncSession = Depends(get_session),
):
    record = await _get_api_key_or_404(key_id, db)
    issued = await api_key_service.regenerate_api_key(record, db)
    return _api_key_created(issued)


@secure_router.post("/api-keys/{key_id}/revoke", response_model=ApiKeyResponse)
async def revoke_api_key(
    key_id: int,
    db: AsyncSession = Depends(get_session),
):
    record = await _get_api_key_or_404(key_id, db)
    record = await api_key_service.revoke_api_key(record, db)
    return _api_key_response(record)
