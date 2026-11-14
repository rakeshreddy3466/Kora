from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any, BinaryIO

CHUNK = 1024 * 1024
HEX64 = frozenset("0123456789abcdef")


def canonical_json(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_stream(fh: BinaryIO) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    while True:
        chunk = fh.read(CHUNK)
        if not chunk:
            break
        h.update(chunk)
        n += len(chunk)
    return h.hexdigest(), n


def sha256_file(path: Path) -> tuple[str, int]:
    with open(path, "rb") as fh:
        return sha256_stream(fh)


def hmac_hex(key: bytes, msg: bytes) -> str:
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def is_sha256(s: str | None) -> bool:
    return bool(s) and len(s) == 64 and set(s) <= HEX64


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"
