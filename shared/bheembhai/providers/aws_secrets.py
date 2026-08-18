"""AWS Secrets Manager — first SecureStorage backend (ADR-012)."""

import boto3

from bheembhai.protocols.secrets import Credential


class AWSSecretsManager:
    """Stores and retrieves credentials from AWS Secrets Manager."""

    backend_name = "aws_secrets_manager"

    def __init__(self, region: str = "us-east-1") -> None:
        self.region = region
        self._client = boto3.client("secretsmanager", region_name=region)

    async def get(self, ref: str) -> Credential | None:
        import botocore.exceptions
        try:
            resp = self._client.get_secret_value(SecretId=ref)
            value = resp.get("SecretString", "")
            return Credential(
                ref=ref,
                value=value,
                provider="aws_secrets_manager",
            )
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                return None
            raise

    async def put(
        self, ref: str, value: str, metadata: dict | None = None
    ) -> str:
        tags = []
        if metadata:
            tags = [{"Key": k, "Value": v} for k, v in metadata.items()]
        resp = self._client.create_secret(
            Name=ref,
            SecretString=value,
            Tags=tags,
        )
        return resp["ARN"]

    async def delete(self, ref: str) -> None:
        import botocore.exceptions
        try:
            self._client.delete_secret(SecretId=ref, ForceDeleteWithoutRecovery=True)
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "ResourceNotFoundException":
                return
            raise
