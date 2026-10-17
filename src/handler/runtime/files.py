"""File storage for workspaces, raw payloads and attachments."""

from __future__ import annotations

import asyncio
import mimetypes
import os
from pathlib import Path, PurePosixPath
from typing import Any, Protocol


class FileStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None: ...

    async def get(self, key: str) -> bytes | None: ...

    async def delete(self, key: str) -> bool: ...

    async def list(self, prefix: str = "") -> list[str]:
        """Keys under `prefix` (a directory-style prefix), sorted."""
        ...

    async def sync_down(self, prefix: str, local_dir: Path) -> int:
        """Download every key under `prefix` into `local_dir`; returns the count."""
        ...

    async def sync_up(self, local_dir: Path, prefix: str) -> int:
        """Upload every file in `local_dir` under `prefix`; returns the count."""
        ...


def clean_key(key: str, *, allow_empty: bool = False) -> str:
    """Normalize a `/`-separated key, rejecting anything that could escape."""
    parts = [part for part in key.split("/") if part not in ("", ".")]
    if ".." in parts or key.startswith("/") or "\\" in key or "\0" in key:
        raise ValueError(f"invalid file key: {key!r}")
    if not parts and not allow_empty:
        raise ValueError("empty file key")
    return "/".join(parts)


def _join(prefix: str, key: str) -> str:
    return f"{prefix}/{key}" if prefix else key


def _local_files(local_dir: Path) -> list[Path]:
    return sorted(p for p in local_dir.rglob("*") if p.is_file() and not p.is_symlink())


class LocalFileStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str, *, allow_empty: bool = False) -> Path:
        path = (self._root / clean_key(key, allow_empty=allow_empty)).resolve()
        if path != self._root and self._root not in path.parents:
            raise ValueError(f"invalid file key: {key!r}")
        return path

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        path = self._path(key)

        def write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
            tmp.write_bytes(data)
            tmp.replace(path)

        await asyncio.to_thread(write)

    async def get(self, key: str) -> bytes | None:
        path = self._path(key)

        def read() -> bytes | None:
            try:
                return path.read_bytes()
            except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
                return None

        return await asyncio.to_thread(read)

    async def delete(self, key: str) -> bool:
        path = self._path(key)

        def remove() -> bool:
            if not path.is_file():
                return False
            path.unlink()
            return True

        return await asyncio.to_thread(remove)

    async def list(self, prefix: str = "") -> list[str]:
        base = self._path(prefix, allow_empty=True)

        def walk() -> list[str]:
            if base.is_file():
                return [base.relative_to(self._root).as_posix()]
            if not base.is_dir():
                return []
            return sorted(
                p.relative_to(self._root).as_posix()
                for p in base.rglob("*") if p.is_file()
            )

        return await asyncio.to_thread(walk)

    async def sync_down(self, prefix: str, local_dir: Path) -> int:
        return await _sync_down(self, prefix, Path(local_dir))

    async def sync_up(self, local_dir: Path, prefix: str) -> int:
        return await _sync_up(self, Path(local_dir), prefix)


async def _sync_down(store: FileStore, prefix: str, local_dir: Path) -> int:
    prefix = clean_key(prefix, allow_empty=True)
    local_dir.mkdir(parents=True, exist_ok=True)
    root = local_dir.resolve()
    count = 0
    for key in await store.list(prefix):
        relative = key[len(prefix):].lstrip("/") if prefix else key
        if not relative:
            continue
        data = await store.get(key)
        if data is None:
            continue
        target = (root / clean_key(relative)).resolve()
        if root not in target.parents:
            raise ValueError(f"invalid file key: {key!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        count += 1
    return count


async def _sync_up(store: FileStore, local_dir: Path, prefix: str) -> int:
    prefix = clean_key(prefix, allow_empty=True)
    count = 0
    for path in _local_files(local_dir):
        key = _join(prefix, path.relative_to(local_dir).as_posix())
        await store.put(key, path.read_bytes(), mimetypes.guess_type(path.name)[0])
        count += 1
    return count


class S3FileStore:
    def __init__(self, bucket: str, prefix: str = "", client: Any = None) -> None:
        if client is None:
            import boto3

            client = boto3.client("s3")
        self._bucket = bucket
        self._prefix = clean_key(prefix, allow_empty=True)
        self._client = client

    @classmethod
    def from_env(cls, prefix: str = "", env: dict[str, str] | None = None) -> S3FileStore:
        import boto3

        env = os.environ if env is None else env
        client = boto3.client(
            "s3",
            aws_access_key_id=env["BUCKETEER_AWS_ACCESS_KEY_ID"],
            aws_secret_access_key=env["BUCKETEER_AWS_SECRET_ACCESS_KEY"],
            region_name=env.get("BUCKETEER_AWS_REGION"),
        )
        return cls(env["BUCKETEER_BUCKET_NAME"], prefix, client)

    def _key(self, key: str, *, allow_empty: bool = False) -> str:
        return _join(self._prefix, clean_key(key, allow_empty=allow_empty))

    async def put(self, key: str, data: bytes, content_type: str | None = None) -> None:
        extra = {"ContentType": content_type} if content_type else {}
        await asyncio.to_thread(
            self._client.put_object, Bucket=self._bucket, Key=self._key(key),
            Body=data, **extra,
        )

    async def get(self, key: str) -> bytes | None:
        def read() -> bytes | None:
            try:
                response = self._client.get_object(Bucket=self._bucket, Key=self._key(key))
            except self._client.exceptions.NoSuchKey:
                return None
            return response["Body"].read()

        return await asyncio.to_thread(read)

    async def delete(self, key: str) -> bool:
        def remove() -> bool:
            full = self._key(key)
            try:
                self._client.head_object(Bucket=self._bucket, Key=full)
            except self._client.exceptions.ClientError as error:
                if error.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                    return False
                raise
            self._client.delete_object(Bucket=self._bucket, Key=full)
            return True

        return await asyncio.to_thread(remove)

    async def list(self, prefix: str = "") -> list[str]:
        directory = clean_key(prefix, allow_empty=True)
        full = self._key(prefix, allow_empty=True)
        search = full + "/" if full and not full.endswith("/") else full
        strip = len(self._prefix) + 1 if self._prefix else 0

        def walk() -> list[str]:
            keys: list[str] = []
            token: str | None = None
            while True:
                args: dict[str, Any] = {"Bucket": self._bucket, "Prefix": search}
                if token:
                    args["ContinuationToken"] = token
                response = self._client.list_objects_v2(**args)
                keys += [item["Key"][strip:] for item in response.get("Contents", [])]
                if not response.get("IsTruncated"):
                    return sorted(keys)
                token = response["NextContinuationToken"]

        keys = await asyncio.to_thread(walk)
        if directory and not keys:
            exact = await self.get(directory)
            return [directory] if exact is not None else []
        return keys

    async def sync_down(self, prefix: str, local_dir: Path) -> int:
        return await _sync_down(self, prefix, Path(local_dir))

    async def sync_up(self, local_dir: Path, prefix: str) -> int:
        return await _sync_up(self, Path(local_dir), prefix)
