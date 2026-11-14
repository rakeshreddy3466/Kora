"""Content-addressed, tiered blob storage.

Tiers
  hot        capsule artifacts, slide store, thumbnails (small, frequently read)
  archive    originals kept for byte-exact / media recovery (cheap, rarely read)
  quarantine copies of items in their deletion grace period

Guarantees
  * key == sha256(plaintext); keys are validated (no path traversal)
  * writes are atomic (temp file + fsync + rename) and idempotent
  * every read is verified against the key; corruption raises BlobIntegrityError
  * optional AES-256-GCM encryption at rest bound to the key (AAD)
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, Protocol

from ..config import get_settings
from ..util import CHUNK, is_sha256
from .crypto import BlobCipher

TIERS = ("hot", "archive", "quarantine")


class BlobIntegrityError(Exception):
    pass


@dataclass(frozen=True)
class BlobRef:
    key: str
    size: int
    tier: str


class BlobStore(Protocol):
    def put_bytes(self, tier: str, data: bytes) -> BlobRef: ...
    def put_file(self, tier: str, path: Path) -> BlobRef: ...
    def put_stream(self, tier: str, fh: BinaryIO) -> BlobRef: ...
    def get_bytes(self, tier: str, key: str) -> bytes: ...
    def write_to(self, tier: str, key: str, dst: Path) -> None: ...
    def exists(self, tier: str, key: str) -> bool: ...
    def size(self, tier: str, key: str) -> int: ...
    def delete(self, tier: str, key: str) -> None: ...
    def verify(self, tier: str, key: str) -> bool: ...
    def iter_keys(self, tier: str) -> Iterator[str]: ...
    def tier_bytes(self, tier: str) -> int: ...


def _check(tier: str, key: str) -> None:
    if tier not in TIERS:
        raise ValueError(f"unknown tier {tier!r}")
    if not is_sha256(key):
        raise ValueError("invalid blob key")


class LocalBlobStore:
    def __init__(self, root: Path, cipher: BlobCipher | None = None):
        self.root = Path(root)
        self.cipher = cipher
        for t in TIERS:
            (self.root / t).mkdir(parents=True, exist_ok=True)

    def _path(self, tier: str, key: str) -> Path:
        _check(tier, key)
        return self.root / tier / key[:2] / key[2:4] / key

    # -- write -------------------------------------------------------------
    def _commit(self, tier: str, key: str, tmp: Path) -> None:
        dst = self._path(tier, key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, dst)
        dfd = os.open(dst.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def _write_tmp(self, tier: str, src: BinaryIO, key: str) -> Path:
        tdir = self.root / tier / ".tmp"
        tdir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=tdir)
        with os.fdopen(fd, "wb") as out:
            if self.cipher:
                self.cipher.encrypt_stream(src, out, key.encode())
            else:
                shutil.copyfileobj(src, out, CHUNK)
            out.flush()
            os.fsync(out.fileno())
        return Path(name)

    def put_stream(self, tier: str, fh: BinaryIO) -> BlobRef:
        # spool once to learn the hash, then commit under the content address
        tdir = self.root / tier / ".tmp"
        tdir.mkdir(parents=True, exist_ok=True)
        h = hashlib.sha256()
        n = 0
        with tempfile.NamedTemporaryFile(dir=tdir, delete=False) as spool:
            while chunk := fh.read(CHUNK):
                h.update(chunk)
                spool.write(chunk)
                n += len(chunk)
        try:
            key = h.hexdigest()
            if self.exists(tier, key):
                return BlobRef(key, n, tier)
            if self.cipher:
                with open(spool.name, "rb") as src:
                    tmp = self._write_tmp(tier, src, key)
                self._commit(tier, key, tmp)
            else:
                self._commit(tier, key, Path(spool.name))
            return BlobRef(key, n, tier)
        finally:
            if os.path.exists(spool.name):
                os.unlink(spool.name)

    def put_bytes(self, tier: str, data: bytes) -> BlobRef:
        key = hashlib.sha256(data).hexdigest()
        _check(tier, key)
        if not self.exists(tier, key):
            tmp = self._write_tmp(tier, io.BytesIO(data), key)
            self._commit(tier, key, tmp)
        return BlobRef(key, len(data), tier)

    def put_file(self, tier: str, path: Path) -> BlobRef:
        with open(path, "rb") as fh:
            return self.put_stream(tier, fh)

    # -- read --------------------------------------------------------------
    def _iter_plain(self, tier: str, key: str) -> Iterator[bytes]:
        p = self._path(tier, key)
        with open(p, "rb") as fh:
            if self.cipher:
                yield from self.cipher.decrypt_stream(fh, key.encode())
            else:
                while chunk := fh.read(CHUNK):
                    yield chunk

    def get_bytes(self, tier: str, key: str) -> bytes:
        self._path(tier, key)                 # validate eagerly: bad keys are caller errors, not corruption
        try:
            data = b"".join(self._iter_plain(tier, key))
        except FileNotFoundError:
            raise
        except Exception as e:  # decryption / auth failure
            raise BlobIntegrityError(f"{tier}/{key}: {e}") from e
        if hashlib.sha256(data).hexdigest() != key:
            raise BlobIntegrityError(f"{tier}/{key}: content does not match address")
        return data

    def write_to(self, tier: str, key: str, dst: Path) -> None:
        h = hashlib.sha256()
        tmp = Path(str(dst) + ".kora-part")
        try:
            with open(tmp, "wb") as out:
                for chunk in self._iter_plain(tier, key):
                    h.update(chunk)
                    out.write(chunk)
            if h.hexdigest() != key:
                raise BlobIntegrityError(f"{tier}/{key}: content does not match address")
            os.replace(tmp, dst)
        finally:
            if tmp.exists():
                tmp.unlink()

    def verify(self, tier: str, key: str) -> bool:
        try:
            h = hashlib.sha256()
            for chunk in self._iter_plain(tier, key):
                h.update(chunk)
            return h.hexdigest() == key
        except Exception:
            return False

    def exists(self, tier: str, key: str) -> bool:
        return self._path(tier, key).is_file()

    def size(self, tier: str, key: str) -> int:
        return self._path(tier, key).stat().st_size

    def delete(self, tier: str, key: str) -> None:
        p = self._path(tier, key)
        if p.exists():
            p.unlink()

    def iter_keys(self, tier: str) -> Iterator[str]:
        base = self.root / tier
        for p in base.glob("*/*/*"):
            if is_sha256(p.name):
                yield p.name

    def tier_bytes(self, tier: str) -> int:
        return sum(self._path(tier, k).stat().st_size for k in self.iter_keys(tier))


class S3BlobStore:
    """S3 / MinIO adapter: one bucket per tier (lifecycle rules per tier).

    Uses SHA-256 checksums end to end (ChecksumAlgorithm) and verifies on read.
    Server-side encryption (SSE-KMS) should be enforced by bucket policy.
    """

    def __init__(self, endpoint: str | None, prefix: str, region: str):
        import boto3  # optional dependency
        self.s3 = boto3.client("s3", endpoint_url=endpoint, region_name=region)
        self.buckets = {t: f"{prefix}-{t}" for t in TIERS}
        for b in self.buckets.values():
            try:
                self.s3.head_bucket(Bucket=b)
            except Exception:
                self.s3.create_bucket(Bucket=b)

    def _k(self, tier: str, key: str) -> tuple[str, str]:
        _check(tier, key)
        return self.buckets[tier], f"{key[:2]}/{key}"

    def put_bytes(self, tier: str, data: bytes) -> BlobRef:
        key = hashlib.sha256(data).hexdigest()
        if not self.exists(tier, key):
            b, k = self._k(tier, key)
            self.s3.put_object(Bucket=b, Key=k, Body=data, ChecksumAlgorithm="SHA256")
        return BlobRef(key, len(data), tier)

    def put_stream(self, tier: str, fh: BinaryIO) -> BlobRef:
        with tempfile.NamedTemporaryFile() as spool:
            h = hashlib.sha256()
            n = 0
            while chunk := fh.read(CHUNK):
                h.update(chunk)
                spool.write(chunk)
                n += len(chunk)
            spool.flush()
            key = h.hexdigest()
            if not self.exists(tier, key):
                b, k = self._k(tier, key)
                self.s3.upload_file(spool.name, b, k, ExtraArgs={"ChecksumAlgorithm": "SHA256"})
            return BlobRef(key, n, tier)

    def put_file(self, tier: str, path: Path) -> BlobRef:
        with open(path, "rb") as fh:
            return self.put_stream(tier, fh)

    def get_bytes(self, tier: str, key: str) -> bytes:
        b, k = self._k(tier, key)
        data = self.s3.get_object(Bucket=b, Key=k)["Body"].read()
        if hashlib.sha256(data).hexdigest() != key:
            raise BlobIntegrityError(f"{tier}/{key}: content does not match address")
        return data

    def write_to(self, tier: str, key: str, dst: Path) -> None:
        b, k = self._k(tier, key)
        tmp = str(dst) + ".kora-part"
        self.s3.download_file(b, k, tmp)
        with open(tmp, "rb") as fh:
            h = hashlib.sha256()
            while chunk := fh.read(CHUNK):
                h.update(chunk)
        if h.hexdigest() != key:
            os.unlink(tmp)
            raise BlobIntegrityError(f"{tier}/{key}: content does not match address")
        os.replace(tmp, dst)

    def verify(self, tier: str, key: str) -> bool:
        try:
            self.get_bytes(tier, key)
            return True
        except Exception:
            return False

    def exists(self, tier: str, key: str) -> bool:
        b, k = self._k(tier, key)
        try:
            self.s3.head_object(Bucket=b, Key=k)
            return True
        except Exception:
            return False

    def size(self, tier: str, key: str) -> int:
        b, k = self._k(tier, key)
        return int(self.s3.head_object(Bucket=b, Key=k)["ContentLength"])

    def delete(self, tier: str, key: str) -> None:
        b, k = self._k(tier, key)
        self.s3.delete_object(Bucket=b, Key=k)

    def iter_keys(self, tier: str) -> Iterator[str]:
        pag = self.s3.get_paginator("list_objects_v2")
        for page in pag.paginate(Bucket=self.buckets[tier]):
            for obj in page.get("Contents", []):
                name = obj["Key"].rsplit("/", 1)[-1]
                if is_sha256(name):
                    yield name

    def tier_bytes(self, tier: str) -> int:
        total = 0
        pag = self.s3.get_paginator("list_objects_v2")
        for page in pag.paginate(Bucket=self.buckets[tier]):
            total += sum(o["Size"] for o in page.get("Contents", []))
        return total


_store: BlobStore | None = None


def get_blobstore() -> BlobStore:
    global _store
    if _store is None:
        s = get_settings()
        if s.blob_backend == "s3":
            _store = S3BlobStore(s.s3_endpoint, s.s3_bucket_prefix, s.s3_region)
        else:
            cipher = BlobCipher(s.blob_encryption_key) if s.blob_encryption_key else None
            _store = LocalBlobStore(s.data_dir / "blobs", cipher)
    return _store


def reset_blobstore() -> None:
    global _store
    _store = None
