from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, UploadFile, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from database import get_session
from schemas import (
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
from services import admin as admin_service
from services.admin.prolific import get_cached_workspace_currency
from auth import require_admin, get_admin_manager
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
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.list_experiments(skip=skip, limit=limit, db=db)


@secure_router.post("/experiments/{experiment_id}/upload")
async def upload_questions(
    experiment_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_session),
):
    return await admin_service.upload_questions_csv(
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
