from __future__ import annotations

import base64
import hmac
import json
import time
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from fastapi import HTTPException

from config import Settings
from services.session_policy import SessionPolicy
from services.rater.session_token import (
    issue_rater_session_token,
    verify_rater_session_token,
)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64url(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def test_issue_token_structure_and_signature() -> None:
    settings = Settings(app_secret_key="test-secret-key")
    token = issue_rater_session_token(
        settings,
        rater_id=123,
        experiment_id=45,
        session_start=datetime.now(UTC),
        policy=SessionPolicy(),
    )

    # v1.<payload>.<sig>
    parts = token.split(".")
    assert len(parts) == 3
    assert parts[0] == "v1"

    payload_b64 = parts[1]
    sig = parts[2]

    # Payload decodes to compact JSON with rid/eid/iat
    payload = json.loads(_unb64url(payload_b64))
    assert payload["rid"] == 123
    assert payload["eid"] == 45
    assert isinstance(payload["iat"], int)

    # Signature is HMAC-SHA256 over the payload using app_secret_key
    expected_sig = _b64url(
        hmac.new(b"test-secret-key", payload_b64.encode("utf-8"), sha256).digest()
    )
    assert sig == expected_sig


def test_verify_roundtrip_and_wrong_key_fails() -> None:
    settings_ok = Settings(app_secret_key="key-ok")
    token = issue_rater_session_token(
        settings_ok,
        rater_id=7,
        experiment_id=9,
        session_start=datetime.now(UTC),
        policy=SessionPolicy(),
    )

    data = verify_rater_session_token(settings_ok, token)
    assert data["rater_id"] == 7
    assert data["experiment_id"] == 9
    assert isinstance(data["issued_at"], int)

    # Verifying with a different secret must fail
    settings_bad = Settings(app_secret_key="key-bad")
    with pytest.raises(HTTPException) as exc:
        verify_rater_session_token(settings_bad, token)
    assert exc.value.status_code == 401


@pytest.mark.parametrize(
    "token",
    [
        "",  # empty
        "v2.foo.bar",  # wrong version
        "v1.onlytwo",  # wrong parts
        "not.a.jwt",  # not in our shape
        "v1..sig",  # empty payload
    ],
)
def test_verify_rejects_invalid_formats(token: str) -> None:
    settings = Settings(app_secret_key="secret")
    with pytest.raises(HTTPException) as exc:
        verify_rater_session_token(settings, token)
    assert exc.value.status_code == 401


def test_verify_rejects_tampered_payload_and_sig() -> None:
    settings = Settings(app_secret_key="secret")
    token = issue_rater_session_token(
        settings,
        rater_id=1,
        experiment_id=2,
        session_start=datetime.now(UTC),
        policy=SessionPolicy(),
    )
    ver, payload_b64, sig = token.split(".")

    # Tamper payload (flip rid) while keeping original sig → should fail
    payload = json.loads(_unb64url(payload_b64))
    payload["rid"] = 999
    tampered_payload_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    tampered_token_same_sig = f"{ver}.{tampered_payload_b64}.{sig}"
    with pytest.raises(HTTPException) as exc1:
        verify_rater_session_token(settings, tampered_token_same_sig)
    assert exc1.value.status_code == 401

    # Tamper signature bytes → should also fail
    tampered_sig = sig[:-1] + ("x" if sig[-1] != "x" else "y")
    tampered_token_bad_sig = f"{ver}.{payload_b64}.{tampered_sig}"
    with pytest.raises(HTTPException) as exc2:
        verify_rater_session_token(settings, tampered_token_bad_sig)
    assert exc2.value.status_code == 401


def test_token_expiry_is_derived_from_session_start_not_issue_time() -> None:
    """`exp` tracks the rater's own session, so re-minting on re-entry cannot
    hand out more time than the session has left.

    Before issue #102 this was `now + rater_session_ttl_seconds`, which meant a
    rater who re-entered near the deadline got a fresh full hour against an old
    `session_start` — the accidental grace period that explicit grace replaces.
    """
    settings = Settings(app_secret_key="ttl-secret")
    policy = SessionPolicy(duration_minutes=60, grace_minutes=5)
    session_start = datetime.now(UTC) - timedelta(minutes=50)

    token = issue_rater_session_token(
        settings,
        rater_id=1,
        experiment_id=2,
        session_start=session_start,
        policy=policy,
    )
    payload = json.loads(_unb64url(token.split(".")[1]))

    # Anchored to the session, not to now: 50 minutes have already elapsed, so
    # what remains is the session's own leftover time, not a fresh full TTL.
    assert payload["exp"] == int(session_start.timestamp()) + policy.token_ttl_seconds
    assert payload["exp"] - int(time.time()) < policy.token_ttl_seconds - 40 * 60


def test_token_covers_the_grace_window() -> None:
    """A token dying on the deadline would reject the very submission the grace
    period exists to accept."""
    settings = Settings(app_secret_key="ttl-secret")
    session_start = datetime.now(UTC)

    without_grace = json.loads(
        _unb64url(
            issue_rater_session_token(
                settings,
                rater_id=1,
                experiment_id=2,
                session_start=session_start,
                policy=SessionPolicy(duration_minutes=60, grace_minutes=0),
            ).split(".")[1]
        )
    )
    with_grace = json.loads(
        _unb64url(
            issue_rater_session_token(
                settings,
                rater_id=1,
                experiment_id=2,
                session_start=session_start,
                policy=SessionPolicy(duration_minutes=60, grace_minutes=5),
            ).split(".")[1]
        )
    )

    assert with_grace["exp"] - without_grace["exp"] == 5 * 60
