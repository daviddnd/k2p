"""Opt-in MinIO round trip for the boto3 storage adapter.

Verifies the ``S3Storage`` adapter against a real S3-compatible store:
put → get round trip and a working pre-signed GET URL. Skipped unless
``AICS_TEST_S3_ENDPOINT_URL`` is set (locally: the compose MinIO at
http://localhost:9000 with the dev credentials). Uses a throwaway bucket
created and deleted by the test itself.
"""

import os
from uuid import uuid4

import boto3
import pytest
from botocore.config import Config

from app.adapters.storage.s3 import S3Storage

ENDPOINT = os.environ.get("AICS_TEST_S3_ENDPOINT_URL")
ACCESS_KEY = os.environ.get("AICS_TEST_S3_ACCESS_KEY", "aics")
SECRET_KEY = os.environ.get("AICS_TEST_S3_SECRET_KEY", "aics-dev-only")

pytestmark = pytest.mark.skipif(
    ENDPOINT is None, reason="set AICS_TEST_S3_ENDPOINT_URL to a running S3-compatible store"
)

PAYLOAD = b"%PDF-1.7 round-trip body"


@pytest.fixture
async def store() -> S3Storage:
    bucket = f"aics-test-{uuid4().hex[:12]}"
    admin = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    admin.create_bucket(Bucket=bucket)
    storage = S3Storage(
        endpoint_url=ENDPOINT or "", bucket=bucket, access_key=ACCESS_KEY, secret_key=SECRET_KEY
    )
    yield storage
    admin.delete_object(Bucket=bucket, Key="some/key.pdf")
    admin.delete_bucket(Bucket=bucket)


async def test_put_get_round_trip(store: S3Storage) -> None:
    await store.put("some/key.pdf", PAYLOAD, "application/pdf")
    assert await store.get_bytes("some/key.pdf") == PAYLOAD


async def test_presigned_url_downloads_the_object(store: S3Storage) -> None:
    import httpx

    await store.put("some/key.pdf", PAYLOAD, "application/pdf")
    url = await store.presign_get("some/key.pdf", expires_in=60)
    response = httpx.get(url)
    assert response.status_code == 200
    assert response.content == PAYLOAD
