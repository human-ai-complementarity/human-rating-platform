"""One way into a private Google Cloud Storage bucket, shared by every reader.

Two readers exist: rater terms (``TERMS__*``) and question media
(``MEDIA__*``). Each carries its own copy of the three Workload Identity
Federation fields, so the identities can differ, and both build their client
here. Two ways in, in order of preference:

1. Workload Identity Federation (keyless), when the WIF audience is set and
   the host provides an OIDC token file (Render writes one per service and
   rotates it). The token is exchanged at Google's STS for a short-lived
   access token, optionally impersonating a service account so the bucket
   grant lives on that one identity. Nothing here is a secret.
2. Application Default Credentials: a service-account key named by
   ``GOOGLE_APPLICATION_CREDENTIALS``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Protocol

GCS_READ_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"
# Signing a URL without a private key calls the IAM credentials API
# (signBlob) as the impersonated service account; that call needs this scope
# on top of the storage one.
IAM_SCOPE = "https://www.googleapis.com/auth/iam"
_STS_TOKEN_URL = "https://sts.googleapis.com/v1/token"
_RENDER_TOKEN_FILE_ENV = "AWS_WEB_IDENTITY_TOKEN_FILE"


class GcsAccess(Protocol):
    """The three WIF fields a settings model carries."""

    gcs_wif_audience: str
    gcs_wif_token_file: str
    gcs_impersonate_service_account: str


class GcsAccessError(Exception):
    """The bucket cannot be reached with the configured identity."""


def wif_token_file(settings: GcsAccess) -> str:
    """Where the host leaves the OIDC identity token for federation."""
    return settings.gcs_wif_token_file or os.environ.get(_RENDER_TOKEN_FILE_ENV, "")


def wif_credentials(
    settings: GcsAccess, token_file: str, scopes: tuple[str, ...] = (GCS_READ_SCOPE,)
):
    """Workload Identity Federation credentials: no key anywhere."""
    from google.auth import identity_pool

    impersonation_url = None
    if settings.gcs_impersonate_service_account:
        impersonation_url = (
            "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/"
            f"{settings.gcs_impersonate_service_account}:generateAccessToken"
        )
    return identity_pool.Credentials(
        audience=settings.gcs_wif_audience,
        subject_token_type="urn:ietf:params:oauth:token-type:jwt",
        token_url=_STS_TOKEN_URL,
        credential_source={"file": token_file},
        service_account_impersonation_url=impersonation_url,
        scopes=list(scopes),
    )


@lru_cache(maxsize=8)
def gcs_client(
    wif_audience: str,
    wif_token_file: str,
    impersonate: str,
    scopes: tuple[str, ...] = (GCS_READ_SCOPE,),
    *,
    setting_prefix: str = "TERMS",
):
    """A storage client for one credential configuration.

    Cached per configuration, so a caller's settings decide the identity
    rather than the process-wide ones. Imported lazily so the dependency is
    only touched when a gs:// source is configured. ``setting_prefix`` names
    the env vars in the error a caller sees.
    """
    import google.auth
    from google.cloud import storage

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if wif_audience:
        if not wif_token_file:
            raise GcsAccessError(
                f"{setting_prefix}__GCS_WIF_AUDIENCE is set but no identity token file was "
                f"found: set {setting_prefix}__GCS_WIF_TOKEN_FILE or let the host set "
                f"{_RENDER_TOKEN_FILE_ENV}"
            )
        credentials = wif_credentials(
            _Access(wif_audience, wif_token_file, impersonate), wif_token_file, scopes
        )
    else:
        credentials, adc_project = google.auth.default()
        project = project or adc_project
    # The client insists on a project, but reading objects never uses it:
    # federated and user credentials carry none (and in a container there is
    # no gcloud binary to ask). Fall back to a placeholder rather than failing
    # a read that would succeed.
    return storage.Client(project=project or "rater-terms-reader", credentials=credentials)


class _Access:
    def __init__(self, audience: str, token_file: str, impersonate: str) -> None:
        self.gcs_wif_audience = audience
        self.gcs_wif_token_file = token_file
        self.gcs_impersonate_service_account = impersonate


def split_gs_url(url: str, *, setting: str) -> tuple[str, str]:
    """``gs://bucket/prefix`` into ``(bucket, prefix)``; the prefix may be empty."""
    bucket_name, _, prefix = url.strip().rstrip("/")[len("gs://") :].partition("/")
    if not bucket_name:
        raise GcsAccessError(f"{setting} gs:// needs a bucket name")
    return bucket_name, prefix


def blob_name_under(prefix: str, relative_path: str) -> str:
    return f"{prefix}/{relative_path}" if prefix else relative_path
