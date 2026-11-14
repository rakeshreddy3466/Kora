"""Embeddings behind a small interface.

Default: an offline feature-hashing embedder (unigrams + bigrams, signed
hashing, sublinear tf, L2-normalised). It needs no model download and no data
leaves the machine - appropriate for sensitive material. Swap in an approved
model endpoint (governed model gateway) by implementing `Embedder`.
"""
from __future__ import annotations

import hashlib
import math

import numpy as np

from .text import tokens

DIM = 512


class Embedder:
    dim = DIM

    def embed(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        toks = tokens(text)
        feats: dict[str, int] = {}
        for t in toks:
            feats[t] = feats.get(t, 0) + 1
        for a, b in zip(toks, toks[1:]):
            k = f"{a} {b}"
            feats[k] = feats.get(k, 0) + 1
        for f, c in feats.items():
            d = hashlib.blake2b(f.encode(), digest_size=8).digest()
            idx = int.from_bytes(d[:4], "little") % self.dim
            sign = 1.0 if d[4] & 1 else -1.0
            v[idx] += sign * (1.0 + math.log(c)) * (0.6 if " " in f else 1.0)
        n = float(np.linalg.norm(v))
        return v / n if n > 0 else v


_default = Embedder()


def embed(text: str) -> np.ndarray:
    return _default.embed(text)


def to_bytes(v: np.ndarray) -> bytes:
    return v.astype(np.float16).tobytes()


def from_bytes(b: bytes | None) -> np.ndarray | None:
    if not b:
        return None
    return np.frombuffer(b, dtype=np.float16).astype(np.float32)


def cosine(a: np.ndarray | None, b: np.ndarray | None) -> float:
    if a is None or b is None:
        return 0.0
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))
