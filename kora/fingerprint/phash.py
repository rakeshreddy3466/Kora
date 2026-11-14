"""Perceptual hashing (64-bit DCT pHash) with vectorised Hamming search.

pHash survives re-encoding, rescaling and mild compression, which is what lets
KORA match a video keyframe to the PDF slide it shows, and recognise the same
recording uploaded to two platforms at different bitrates."""
from __future__ import annotations

import numpy as np
from PIL import Image

_N = 32
_k = np.arange(_N)
_DCT = np.sqrt(2.0 / _N) * np.cos(np.pi * (2 * _k[None, :] + 1) * _k[:, None] / (2 * _N))
_DCT[0, :] = np.sqrt(1.0 / _N)


def phash(img: Image.Image) -> str:
    g = img.convert("L").resize((_N, _N), Image.Resampling.LANCZOS)
    a = np.asarray(g, dtype=np.float64)
    d = _DCT @ a @ _DCT.T
    low = d[:8, :8].flatten()
    med = np.median(low[1:])
    bits = low > med
    v = 0
    for b in bits:
        v = (v << 1) | int(b)
    return f"{v:016x}"


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def to_array(hashes: list[str]) -> np.ndarray:
    return np.array([int(h, 16) for h in hashes], dtype=np.uint64)


def _popcount64(x: np.ndarray) -> np.ndarray:
    x = x - ((x >> np.uint64(1)) & np.uint64(0x5555555555555555))
    x = (x & np.uint64(0x3333333333333333)) + ((x >> np.uint64(2)) & np.uint64(0x3333333333333333))
    x = (x + (x >> np.uint64(4))) & np.uint64(0x0F0F0F0F0F0F0F0F)
    return ((x * np.uint64(0x0101010101010101)) >> np.uint64(56)).astype(np.int64)


def hamming_many(query: str, arr: np.ndarray) -> np.ndarray:
    if arr.size == 0:
        return np.zeros(0, dtype=np.int64)
    return _popcount64(np.bitwise_xor(arr, np.uint64(int(query, 16))))
