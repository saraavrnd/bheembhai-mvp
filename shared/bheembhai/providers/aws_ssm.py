"""AWS SSM Parameter Store — lightweight SecureStorage backend (ADR-012).

For DEV/MVP: free, same boto3 surface as Secrets Manager, no rotation needed.
Production can swap to aws_secrets_manager by changing one env var.
"""

import boto3

from bheembhai.protocols.secrets import Credential


class AWSSSMParameterStore:
    """Stores and retrieves credentials from AWS SSM Parameter Store (SecureString)."""

    backend_name = "aws_ssm"

    def __init__(self, region: str = "us-east-1") -> None:
        self.region = region
        self._client = boto3.client("ssm", region_name=region)

    async def get(self, ref: str) -> Credential | None:
        """Retrieve a SecureString parameter.  Returns None if not found."""
        import botocore.exceptions

        try:
            resp = self._client.get_parameter(Name=ref, WithDecryption=True)
            value = resp["Parameter"]["Value"]
            return Credential(
                ref=ref,
                value=value,
                provider="aws_ssm",
            )
        except botocore.exceptions.ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "ParameterNotFound":
                return None
            raise

    async def put(
        self, ref: str, value: str, metadata: dict | None = None
    ) -> str:
        """Store a SecureString parameter.  Returns the parameter name as the ref.

        AWS SSM forbids ``Overwrite=True`` together with ``Tags``, so we split
        into two cases: first-write (tags allowed) vs overwrite (no tags).
        """
        import botocore.exceptions

        tags = []
        if metadata:
            tags = [{"Key": k, "Value": v} for k, v in metadata.items()]

        try:
            # First, try creating without Overwrite — tags are OK on creation.
            self._client.put_parameter(
                Name=ref,
                Value=value,
                Type="SecureString",
                Overwrite=False,
                Tags=tags,
            )
        except botocore.exceptions.ClientError as e:
            code = e.response["Error"]["Code"]
            if code == "ParameterAlreadyExists":
                # Must overwrite without tags — AWS rejects tags+overwrite together.
                self._client.put_parameter(
                    Name=ref,
                    Value=value,
                    Type="SecureString",
                    Overwrite=True,
                )
            else:
                raise
        return ref

    async def delete(self, ref: str) -> None:
        """Delete a parameter.  No-op if not found."""
        import botocore.exceptions

        try:
            self._client.delete_parameter(Name=ref)
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "ParameterNotFound":
                return
            raise
