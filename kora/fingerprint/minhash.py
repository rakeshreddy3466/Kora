"""MinHash signatures + LSH banding for near-duplicate detection without O(n^2).

128 permutations, 32 bands x 4 rows: candidate probability crosses 50% around
Jaccard ~0.42, so true near-duplicates (J >= 0.85) are found with ~100%
probability while unrelated documents rarely collide."""
from __future__ import annotations

import hashlib

import numpy as np

NUM_PERM = 128
BANDS = 32
ROWS = NUM_PERM // BANDS
_PRIME = np.uint64((1 << 61) - 1)
_MAX = np.uint64((1 << 32) - 1)
_rng = np.random.RandomState(0x4B4F5241)   # "KORA": fixed seed -> stable signatures across runs
_A = _rng.randint(1, 2 ** 31 - 1, size=NUM_PERM, dtype=np.int64).astype(np.uint64)
_B = _rng.randint(0, 2 ** 31 - 1, size=NUM_PERM, dtype=np.int64).astype(np.uint64)


def _h32(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode(), digest_size=4).digest(), "little")


def signature(shingles: set[str]) -> np.ndarray:
    if not shingles:
        return np.full(NUM_PERM, _MAX, dtype=np.uint64)
    hv = np.array([_h32(x) for x in shingles], dtype=np.uint64)
    perm = (np.outer(hv, _A) + _B) % _PRIME & _MAX
    return perm.min(axis=0).astype(np.uint64)


def to_bytes(sig: np.ndarray) -> bytes:
    return sig.astype(np.uint32).tobytes()


def from_bytes(b: bytes | None) -> np.ndarray | None:
    if not b:
        return None
    return np.frombuffer(b, dtype=np.uint32).astype(np.uint64)


def jaccard(a: np.ndarray | None, b: np.ndarray | None) -> float:
    if a is None or b is None:
        return 0.0
    if np.all(a == _MAX) or np.all(b == _MAX):
        return 0.0
    return float(np.mean(a == b))


def bands(sig: np.ndarray) -> list[tuple[int, str]]:
    out = []
    for i in range(BANDS):
        chunk = sig[i * ROWS:(i + 1) * ROWS].astype(np.uint32).tobytes()
        out.append((i, hashlib.blake2b(chunk, digest_size=8).hexdigest()))
    return out
