from __future__ import annotations

import logging
from dataclasses import dataclass

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
    # Whether the rater has agreed to the consent statement. Read from the
    # rater row, not the token, so it cannot be forged or go stale.
    consented: bool = False


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

    return RaterSession(
        rater_id=data["rater_id"],
        experiment_id=data["experiment_id"],
        issued_at=data["issued_at"],
        expires_at=data["expires_at"],
        consented=(await fetch_consent_record(rater.id, db)) is not None,
    )


async def require_consented_rater_session(
    session: RaterSession = Depends(require_rater_session),
) -> RaterSession:
    """A rater session whose owner has agreed to the consent statement.

    Everything that serves or accepts study content hangs off this, so the
    consent screen cannot be skipped by calling the API directly.
    """
    if not session.consented:
        raise HTTPException(status_code=403, detail="Consent required")
    return session
