from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from fastapi import Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from config import Settings, get_settings
from database import get_session
from services.rater.queries import fetch_consent_record, fetch_rater_or_404
from services.rater.session_token import verify_rater_session_token

logger = logging.getLogger(__name__)


@dataclass
class RaterSession:
    rater_id: int
    experiment_id: int
    issued_at: int
    expires_at: int
    session_generation: str | None = None


async def require_rater_session(
    x_rater_session: str = Header(..., alias="X-Rater-Session"),
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_session),
) -> RaterSession:
    """Verify the rater session token and bind it to server-side state.

    - Validates signature and TTL
    - Ensures the token's experiment_id matches the rater's persisted experiment_id
    """
    data = verify_rater_session_token(settings, x_rater_session)

    rater = await fetch_rater_or_404(data["rater_id"], db)
    if rater.experiment_id != data["experiment_id"]:
        # Token claim does not match server-side state
        logger.warning(
            "Rater session experiment_id mismatch",
            extra={
                "attributes": {
                    "rater_id": data["rater_id"],
                    "token_experiment_id": data["experiment_id"],
                    "actual_experiment_id": rater.experiment_id,
                }
            },
        )
        raise HTTPException(status_code=401, detail="Invalid rater session")

    generation = data.get("session_generation")
    if generation is not None and getattr(rater, "session_start", None) is not None:
        try:
            reset = rater.session_start > datetime.fromisoformat(generation)
        except (ValueError, TypeError):
            raise HTTPException(401, "Invalid rater session")
        if reset:
            raise HTTPException(401, "Rater session was reset")

    # Preserve the identity authenticated before any later lock wait or refresh.
    # SQLAlchemy identity-map objects are weakly held; they are not an auth snapshot.
    db.info["authenticated_rater_generation"] = (rater.id, rater.session_start)

    return RaterSession(
        rater_id=data["rater_id"],
        experiment_id=data["experiment_id"],
        issued_at=data["issued_at"],
        expires_at=data["expires_at"],
        session_generation=generation,
    )


async def require_consented_rater_session(
    session: RaterSession = Depends(require_rater_session),
    db: AsyncSession = Depends(get_session),
) -> RaterSession:
    """A rater session whose owner has agreed to the consent statement.

    Everything that serves or accepts study content hangs off this, so the
    consent screen cannot be skipped by calling the API directly. Checked
    against the consent table here, not in the token, so it cannot be forged
    or go stale; endpoints that only do session bookkeeping (status polling,
    end-session) use the plain dependency and skip the lookup.
    """
    if await fetch_consent_record(session.rater_id, db) is None:
        raise HTTPException(status_code=403, detail="Consent required")
    return session
