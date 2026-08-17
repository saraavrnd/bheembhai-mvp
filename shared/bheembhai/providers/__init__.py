"""Concrete provider implementations."""

from bheembhai.providers.cognito import CognitoProvider
from bheembhai.providers.cognito_auth import (
    AuthError,
    AuthTokens,
    CognitoAuthService,
)
from bheembhai.providers.s3_storage import S3Storage
from bheembhai.providers.local_storage import LocalStorage
from bheembhai.providers.aws_secrets import AWSSecretsManager
from bheembhai.providers.aws_ssm import AWSSSMParameterStore
from bheembhai.providers.env_secrets import EnvSecureStorage

def build_object_store(storage_cfg):
    """ADR-011: pick the ObjectStorage backend for a StorageConfig.

    Local filesystem by default (dev without AWS setup); S3 via
    STORAGE_BACKEND=s3 + S3_BUCKET (+ S3_REGION, S3_ENDPOINT_URL for
    MinIO/LocalStack). Azure Blob is deferred.
    """
    if storage_cfg.backend == "s3":
        return S3Storage(
            bucket=storage_cfg.s3_bucket,
            region=storage_cfg.s3_region,
            endpoint_url=storage_cfg.s3_endpoint_url or None,
        )
    if storage_cfg.backend == "minio":
        return S3Storage(
            bucket=storage_cfg.minio_bucket,
            region="us-east-1",
            endpoint_url=f"http://{storage_cfg.minio_endpoint}",
        )
    return LocalStorage(base_path=storage_cfg.local_base_path)


__all__ = [
    "CognitoProvider",
    "AuthError",
    "AuthTokens",
    "CognitoAuthService",
    "S3Storage",
    "LocalStorage",
    "AWSSecretsManager",
    "AWSSSMParameterStore",
    "EnvSecureStorage",
    "build_object_store",
]
