"""Content-addressed artifact storage behind one interface, with two backends.

* ``FsArtifactStore`` — a directory (``<root>/<sha[:2]>/<sha>``), for local development, tests and a
  single-node volume.
* ``S3ArtifactStore`` — any S3-compatible endpoint (Cloudflare R2, MinIO, AWS) with a PRIVATE bucket.

Objects are keyed by the sha256 of their bytes and written once; ``put_file`` verifies the digest it is
given. Nothing here produces a public URL: bytes reach clients only through the API's signed URLs.
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol

from .settings import Settings

SHA256 = re.compile(r"^[0-9a-f]{64}$")
CHUNK = 1024 * 1024


class ArtifactDigestMismatch(ValueError):
    pass


class ArtifactNotFound(LookupError):
    pass


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _check(sha256: str) -> str:
    if not SHA256.fullmatch(sha256):
        raise ValueError("not a sha256 hex digest")
    return sha256


class ArtifactStore(Protocol):
    def put_file(self, path: Path, sha256: str, media_type: str) -> None: ...
    def exists(self, sha256: str) -> bool: ...
    def size(self, sha256: str) -> int: ...
    def open_stream(self, sha256: str) -> Iterator[bytes]: ...


class FsArtifactStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, sha256: str) -> Path:
        sha256 = _check(sha256)
        return self.root / sha256[:2] / sha256

    def put_file(self, path: Path, sha256: str, media_type: str) -> None:
        if file_sha256(path) != sha256:
            raise ArtifactDigestMismatch("file bytes do not match the declared sha256")
        dest = self._path(sha256)
        if dest.exists():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".incoming-")
        os.close(fd)
        try:
            shutil.copyfile(path, tmp)
            os.replace(tmp, dest)  # atomic: readers never see a partial object
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def exists(self, sha256: str) -> bool:
        return self._path(sha256).is_file()

    def size(self, sha256: str) -> int:
        path = self._path(sha256)
        if not path.is_file():
            raise ArtifactNotFound(sha256)
        return path.stat().st_size

    def open_stream(self, sha256: str) -> Iterator[bytes]:
        path = self._path(sha256)
        if not path.is_file():
            raise ArtifactNotFound(sha256)

        def gen() -> Iterator[bytes]:
            with path.open("rb") as fh:
                yield from iter(lambda: fh.read(CHUNK), b"")

        return gen()


class S3ArtifactStore:
    def __init__(self, client, bucket: str, prefix: str = "artifacts/"):
        self.client = client
        self.bucket = bucket
        self.prefix = prefix

    @classmethod
    def from_settings(cls, s: Settings) -> S3ArtifactStore:
        import boto3
        from botocore.config import Config

        if not s.s3_bucket:
            raise RuntimeError("S3_BUCKET is not set")
        client = boto3.client(
            "s3",
            endpoint_url=s.s3_endpoint_url or None,
            region_name=s.s3_region,
            aws_access_key_id=s.s3_access_key_id or None,
            aws_secret_access_key=s.s3_secret_access_key or None,
            config=Config(
                signature_version="s3v4",
                retries={"max_attempts": 3, "mode": "standard"},
                connect_timeout=5,
                read_timeout=60,
            ),
        )
        return cls(client, s.s3_bucket, s.s3_prefix)

    def _key(self, sha256: str) -> str:
        sha256 = _check(sha256)
        return f"{self.prefix}{sha256[:2]}/{sha256}"

    def put_file(self, path: Path, sha256: str, media_type: str) -> None:
        if file_sha256(path) != sha256:
            raise ArtifactDigestMismatch("file bytes do not match the declared sha256")
        if self.exists(sha256):
            return
        with path.open("rb") as fh:
            # ChecksumSHA256 makes the store verify the bytes it received as well.
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._key(sha256),
                Body=fh,
                ContentType=media_type,
                ChecksumSHA256=base64.b64encode(bytes.fromhex(sha256)).decode(),
            )

    def _head(self, sha256: str):
        from botocore.exceptions import ClientError

        try:
            return self.client.head_object(Bucket=self.bucket, Key=self._key(sha256))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def exists(self, sha256: str) -> bool:
        return self._head(sha256) is not None

    def size(self, sha256: str) -> int:
        head = self._head(sha256)
        if head is None:
            raise ArtifactNotFound(sha256)
        return int(head["ContentLength"])

    def open_stream(self, sha256: str) -> Iterator[bytes]:
        from botocore.exceptions import ClientError

        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self._key(sha256))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                raise ArtifactNotFound(sha256) from None
            raise
        return obj["Body"].iter_chunks(CHUNK)


def store_from_settings(s: Settings) -> ArtifactStore:
    if s.artifact_backend == "s3":
        return S3ArtifactStore.from_settings(s)
    return FsArtifactStore(s.artifact_fs_root)
