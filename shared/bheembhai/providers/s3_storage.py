"""S3 object storage — first ObjectStorage backend (ADR-011).

AWS credentials come from boto3's default credential chain — env vars /
shared credentials file / IAM instance role — never from application config.
The engine (upload) and the platform (read) should get separate IAM policies;
the agent containers get no AWS credentials at all.
"""

from collections.abc import AsyncIterator

import boto3

from bheembhai.protocols.storage import (
    PresignedUrl,
    StoredHead,
    StoredObject,
)


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

    async def put_file(
        self, key: str, path: str, content_type: str | None = None
    ) -> None:
        # upload_file streams from disk (multipart for large logs) — no
        # buffering of a 10 MB agent.log in process memory.
        extra_args = {"ContentType": content_type} if content_type else None
        self._client.upload_file(
            path, self.bucket, key, ExtraArgs=extra_args
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

    async def head(self, key: str) -> StoredHead | None:
        import botocore.exceptions
        try:
            resp = self._client.head_object(Bucket=self.bucket, Key=key)
            return StoredHead(key=key, size=int(resp.get("ContentLength", 0)))
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
                return None
            raise

    async def get_range(
        self, key: str, start: int = 0, end: int | None = None
    ) -> bytes:
        import botocore.exceptions
        if end is not None and end < start:
            return b""
        rng = f"bytes={start}-{end if end is not None else ''}"
        try:
            resp = self._client.get_object(
                Bucket=self.bucket, Key=key, Range=rng
            )
            return resp["Body"].read()
        except botocore.exceptions.ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("404", "NoSuchKey", "InvalidRange"):
                return b""
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

    async def presigned_put_url(
        self, key: str, expires_in: int = 3600
    ) -> PresignedUrl:
        import time
        url = self._client.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires_in,
        )
        return PresignedUrl(url=url, expires_at=time.time() + expires_in)

    async def list(self, prefix: str) -> AsyncIterator[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]
