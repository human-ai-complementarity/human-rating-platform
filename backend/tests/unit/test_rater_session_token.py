from __future__ import annotations

import base64
import hmac
import json
from hashlib import sha256

import pytest
from fastapi import HTTPException

from config import Settings
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
    token = issue_rater_session_token(settings, rater_id=123, experiment_id=45)

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
    token = issue_rater_session_token(settings_ok, rater_id=7, experiment_id=9)

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
    token = issue_rater_session_token(settings, rater_id=1, experiment_id=2)
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
