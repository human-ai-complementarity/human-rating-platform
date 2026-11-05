"""One way into a private Google Cloud Storage bucket, shared by every reader.

Two readers exist: rater terms (``TERMS__*``) and question media
(``MEDIA__*``). Each settings model carries the same ``GcsAccessSettings``
fields, so the identities can differ, and both build their credentials and
client here. Two ways in, in order of preference:

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

from config import GcsAccessSettings

GCS_READ_SCOPE = "https://www.googleapis.com/auth/devstorage.read_only"
_STS_TOKEN_URL = "https://sts.googleapis.com/v1/token"
_RENDER_TOKEN_FILE_ENV = "AWS_WEB_IDENTITY_TOKEN_FILE"

Scopes = tuple[str, ...]
READ_SCOPES: Scopes = (GCS_READ_SCOPE,)


class GcsAccessError(Exception):
    """The bucket cannot be reached with the configured identity."""


def wif_token_file(settings: GcsAccessSettings) -> str:
    """Where the host leaves the OIDC identity token for federation."""
    return settings.gcs_wif_token_file or os.environ.get(_RENDER_TOKEN_FILE_ENV, "")


def wif_credentials(settings: GcsAccessSettings, token_file: str, scopes: Scopes = READ_SCOPES):
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
def gcs_credentials(
    wif_audience: str,
    wif_token_file: str,
    impersonate: str,
    scopes: Scopes = READ_SCOPES,
    *,
    setting_prefix: str = "TERMS",
):
    """The credentials for one access configuration, and their project.

    Cached per configuration, so a caller's settings decide the identity
    rather than the process-wide ones. Imported lazily so the dependency is
    only touched when a gs:// source is configured. ``setting_prefix`` names
    the env vars in the error a caller sees.
    """
    import google.auth

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if wif_audience:
        if not wif_token_file:
            raise GcsAccessError(
                f"{setting_prefix}__GCS_WIF_AUDIENCE is set but no identity token file was "
                f"found: set {setting_prefix}__GCS_WIF_TOKEN_FILE or let the host set "
                f"{_RENDER_TOKEN_FILE_ENV}"
            )
        settings = GcsAccessSettings(
            gcs_wif_audience=wif_audience,
            gcs_wif_token_file=wif_token_file,
            gcs_impersonate_service_account=impersonate,
        )
        return wif_credentials(settings, wif_token_file, scopes), project
    credentials, adc_project = google.auth.default(scopes=list(scopes))
    return credentials, project or adc_project


@lru_cache(maxsize=8)
def gcs_client(
    wif_audience: str,
    wif_token_file: str,
    impersonate: str,
    scopes: Scopes = READ_SCOPES,
    *,
    setting_prefix: str = "TERMS",
):
    """A storage client for one access configuration (see ``gcs_credentials``)."""
    from google.cloud import storage

    credentials, project = gcs_credentials(
        wif_audience, wif_token_file, impersonate, scopes, setting_prefix=setting_prefix
    )
    # The client insists on a project, but reading objects never uses it:
    # federated and user credentials carry none (and in a container there is
    # no gcloud binary to ask). Fall back to a placeholder rather than failing
    # a read that would succeed.
    return storage.Client(project=project or "rater-terms-reader", credentials=credentials)


def clear_cached_clients() -> None:
    """Forget cached credentials and clients, after one of them was refused."""
    gcs_credentials.cache_clear()
    gcs_client.cache_clear()


def access_key(settings: GcsAccessSettings) -> tuple[str, str, str]:
    """The positional cache key for ``settings``' identity."""
    return (
        settings.gcs_wif_audience,
        wif_token_file(settings),
        settings.gcs_impersonate_service_account,
    )


def split_gs_url(url: str, *, setting: str) -> tuple[str, str]:
    """``gs://bucket/prefix`` into ``(bucket, prefix)``; the prefix may be empty."""
    bucket_name, _, prefix = url.strip().rstrip("/")[len("gs://") :].partition("/")
    if not bucket_name:
        raise GcsAccessError(f"{setting} gs:// needs a bucket name")
    return bucket_name, prefix


def blob_name_under(prefix: str, relative_path: str) -> str:
    return f"{prefix}/{relative_path}" if prefix else relative_path
