"""A thin async wrapper over boto3 for MinIO.

boto3 is synchronous; every call runs in a worker thread so the event loop
stays free. The API process streams downloads through `iter_object` instead
of buffering whole files.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from swarn_backend.settings import Settings
from swarn_backend.storage.keys import content_type_for

CHUNK = 1 << 20


class ObjectStore:
    def __init__(self, settings: Settings):
        self.bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                          retries={"max_attempts": 3}),
        )

    # ── sync primitives (run in threads) ────────────────────────────────

    def _ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self.bucket)

    def _put_bytes(self, key: str, data: bytes, content_type: str | None) -> None:
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data,
                                ContentType=content_type or content_type_for(key))

    def _put_file(self, key: str, path: str, content_type: str | None) -> int:
        size = os.path.getsize(path)
        self._client.upload_file(path, self.bucket, key,
                                 ExtraArgs={"ContentType": content_type or content_type_for(key)})
        return size

    def _put_fileobj(self, key: str, fileobj, content_type: str | None) -> None:
        self._client.upload_fileobj(fileobj, self.bucket, key,
                                    ExtraArgs={"ContentType": content_type or content_type_for(key)})

    def _get_bytes(self, key: str) -> bytes | None:
        try:
            obj = self._client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise
        return obj["Body"].read()

    def _head(self, key: str) -> dict | None:
        try:
            return self._client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                return None
            raise

    def _list(self, prefix: str) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []) or []:
                out.append((item["Key"], int(item.get("Size", 0))))
        return out

    def _download(self, key: str, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(self.bucket, key, path)

    def _delete_keys(self, keys: list[str]) -> None:
        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
            self._client.delete_objects(Bucket=self.bucket,
                                        Delete={"Objects": [{"Key": k} for k in batch],
                                                "Quiet": True})

    # ── async surface ───────────────────────────────────────────────────

    async def ensure_bucket(self) -> None:
        await asyncio.to_thread(self._ensure_bucket)

    async def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        await asyncio.to_thread(self._put_bytes, key, data, content_type)

    async def put_file(self, key: str, path: str, content_type: str | None = None) -> int:
        return await asyncio.to_thread(self._put_file, key, path, content_type)

    async def put_fileobj(self, key: str, fileobj, content_type: str | None = None) -> None:
        await asyncio.to_thread(self._put_fileobj, key, fileobj, content_type)

    async def get_bytes(self, key: str) -> bytes | None:
        return await asyncio.to_thread(self._get_bytes, key)

    async def head(self, key: str) -> dict | None:
        return await asyncio.to_thread(self._head, key)

    async def exists(self, key: str) -> bool:
        return (await self.head(key)) is not None

    async def list_keys(self, prefix: str) -> list[tuple[str, int]]:
        return await asyncio.to_thread(self._list, prefix)

    async def download(self, key: str, path: str) -> None:
        await asyncio.to_thread(self._download, key, path)

    async def download_prefix(self, prefix: str, dest_dir: str, max_bytes: int | None = None) -> int:
        """Mirror every object under `prefix` into `dest_dir`. Returns bytes."""
        total = 0
        for key, size in await self.list_keys(prefix):
            rel = key[len(prefix):]
            if not rel or rel.endswith("/"):
                continue
            total += size
            if max_bytes is not None and total > max_bytes:
                raise RuntimeError(f"staging {prefix} exceeds the size cap")
            await self.download(key, os.path.join(dest_dir, rel))
        return total

    async def upload_dir(self, src_dir: str, prefix: str, skip=None) -> list[tuple[str, str, int]]:
        """Upload every file under `src_dir` to `prefix`. Returns (rel, key, size)."""
        out: list[tuple[str, str, int]] = []
        for root, _dirs, files in os.walk(src_dir):
            for name in files:
                full = os.path.join(root, name)
                rel = os.path.relpath(full, src_dir).replace(os.sep, "/")
                if skip and skip(rel):
                    continue
                key = prefix + rel
                size = await self.put_file(key, full)
                out.append((rel, key, size))
        return out

    async def delete_prefix(self, prefix: str) -> int:
        keys = [k for k, _ in await self.list_keys(prefix)]
        if keys:
            await asyncio.to_thread(self._delete_keys, keys)
        return len(keys)

    async def delete_key(self, key: str) -> None:
        await asyncio.to_thread(self._delete_keys, [key])

    async def iter_object(self, key: str, chunk: int = CHUNK) -> AsyncIterator[bytes]:
        """Stream an object in chunks without holding it in memory."""
        obj = await asyncio.to_thread(self._client.get_object, Bucket=self.bucket, Key=key)
        body = obj["Body"]
        try:
            while True:
                data = await asyncio.to_thread(body.read, chunk)
                if not data:
                    break
                yield data
        finally:
            body.close()


_store: ObjectStore | None = None


def get_store() -> ObjectStore:
    global _store
    if _store is None:
        from swarn_backend.settings import get_settings
        _store = ObjectStore(get_settings())
    return _store
