"""S3 object storage — first ObjectStorage backend (ADR-011)."""

from typing import AsyncIterator

import boto3

from bheembhai.protocols.storage import ObjectStorage, PresignedUrl, StoredObject


class S3Storage:
    """Stores artifacts in an S3 bucket."""

    backend_name = "s3"

    def __init__(
        self,
        bucket: str,
        region: str = "us-east-1",
        endpoint_url: str | None = None,
    ) -> None:
        self.bucket = bucket
        self.region = region
        self._client = boto3.client(
            "s3",
            region_name=region,
            endpoint_url=endpoint_url or None,
        )

    async def put(
        self, key: str, data: bytes, content_type: str | None = None
    ) -> None:
        extra_args = {}
        if content_type:
            extra_args["ContentType"] = content_type
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            **extra_args,
        )

    async def get(self, key: str) -> StoredObject | None:
        import botocore.exceptions
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=key)
            return StoredObject(
                key=key,
                data=resp["Body"].read(),
                content_type=resp.get("ContentType"),
                metadata=resp.get("Metadata"),
            )
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise

    async def presigned_get_url(
        self, key: str, expires_in: int = 3600
    ) -> PresignedUrl:
        import time
        url = self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_in,
        )
        return PresignedUrl(url=url, expires_at=time.time() + expires_in)

    async def list(self, prefix: str) -> AsyncIterator[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]
