"""Original uploaded bytes.

The database stores the key. The bytes live here. local writes a directory
on this machine. s3 speaks the S3 API, which MinIO and AWS S3 both accept.
"""

from __future__ import annotations

import os
from pathlib import Path


class ObjectStore:
    def put(self, key: str, data: bytes) -> None:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError


class LocalObjectStore(ObjectStore):
    def __init__(self, root: str | None = None):
        self.root = Path(
            root
            or os.getenv("OBJECT_STORE_PATH")
            or str(Path(__file__).resolve().parents[1] / "data" / "objects")
        )

    def _path(self, key: str) -> Path:
        relative = Path(key)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Object key is not valid")
        return self.root / relative

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()


class S3ObjectStore(ObjectStore):
    def __init__(self):
        import boto3

        self.bucket = os.getenv("S3_BUCKET")
        if not self.bucket:
            raise RuntimeError("S3_BUCKET is not set.")
        self.client = boto3.client(
            "s3",
            endpoint_url=os.getenv("S3_ENDPOINT_URL") or None,
            aws_access_key_id=os.getenv("S3_ACCESS_KEY_ID") or None,
            aws_secret_access_key=os.getenv("S3_SECRET_ACCESS_KEY") or None,
            region_name=os.getenv("S3_REGION", "us-east-1"),
        )

    def put(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def get(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read()


def get_object_store() -> ObjectStore:
    backend = os.getenv("OBJECT_STORE_BACKEND", "local").lower()
    if backend == "local":
        return LocalObjectStore()
    if backend == "s3":
        return S3ObjectStore()
    raise ValueError(
        f"Unknown OBJECT_STORE_BACKEND='{backend}'. Supported values: local, s3"
    )
