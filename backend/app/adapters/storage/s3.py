"""S3-compatible blob storage adapter (ADR-0009: blobs in MinIO).

The only module that talks to the object store. boto3 is synchronous; the
async methods here offload each call to a thread so the event loop is never
blocked by an upload or download. Files are served via short-lived
pre-signed URLs only (ADR-0009: API-mediated access) — this adapter never
exposes the store directly.

Path-style addressing is forced: the deployment target is MinIO
(``deploy/docker-compose.yml``), where virtual-hosted-style bucket names
do not resolve.
"""

import asyncio

import boto3
from botocore.config import Config

# ADR-0009: short-lived pre-signed URLs.
DEFAULT_PRESIGN_TTL_SECONDS = 900  # 15 minutes


class S3Storage:
    """The ``DocumentStore`` implementation backed by S3/MinIO."""

    def __init__(
        self, *, endpoint_url: str, bucket: str, access_key: str, secret_key: str
    ) -> None:
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        )

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        """Write a new object. Keys are never rewritten (WORM — ADR-0009):
        callers render a fresh key per object; this method does not guard
        against overwrite, the registry's unique ``storage_key`` does."""
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )

    async def get_bytes(self, key: str) -> bytes:
        """The object's bytes — used by server-side processing (extraction)
        and the ADR-0011 integrity re-hash job."""
        response = await asyncio.to_thread(
            self._client.get_object, Bucket=self._bucket, Key=key
        )
        return await asyncio.to_thread(response["Body"].read)

    async def presign_get(self, key: str, *, expires_in: int) -> str:
        """A short-lived pre-signed GET URL for one object."""
        return await asyncio.to_thread(
            self._client.generate_presigned_url,
            "get_object",
            Params={"Bucket": self._bucket, "Key": key},
            ExpiresIn=expires_in,
        )
