"""Chunked AES-256-GCM envelope for blobs at rest (STREAM-style construction).

Layout:  b"KENC1" | nonce_prefix(7) | { ciphertext(chunk)+tag(16) }*
Nonce per chunk = nonce_prefix(7) || counter(4, big endian) || last_flag(1).
AAD = blob key, binding ciphertext to its content address so blobs cannot be
swapped. Truncation is detected because the final chunk carries last_flag=1.
Works for multi-GB recordings without loading them into memory.
"""
from __future__ import annotations

import os
import struct
from typing import BinaryIO, Iterator

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"KENC1"
CHUNK = 1024 * 1024
TAG = 16


class BlobCipher:
    def __init__(self, key: bytes):
        if len(key) < 32:
            raise ValueError("encryption key must be 32 bytes")
        self._aead = AESGCM(key[:32])

    @staticmethod
    def _nonce(prefix: bytes, counter: int, last: bool) -> bytes:
        return prefix + struct.pack(">I", counter) + (b"\x01" if last else b"\x00")

    def encrypt_stream(self, src: BinaryIO, dst: BinaryIO, aad: bytes) -> None:
        prefix = os.urandom(7)
        dst.write(MAGIC + prefix)
        counter = 0
        chunk = src.read(CHUNK)
        while True:
            nxt = src.read(CHUNK)
            last = not nxt
            dst.write(self._aead.encrypt(self._nonce(prefix, counter, last), chunk, aad))
            if last:
                return
            chunk, counter = nxt, counter + 1

    def decrypt_stream(self, src: BinaryIO, aad: bytes) -> Iterator[bytes]:
        head = src.read(len(MAGIC) + 7)
        if not head.startswith(MAGIC):
            raise ValueError("not an encrypted KORA blob")
        prefix = head[len(MAGIC):]
        counter = 0
        block = src.read(CHUNK + TAG)
        while block:
            nxt = src.read(CHUNK + TAG)
            last = not nxt
            yield self._aead.decrypt(self._nonce(prefix, counter, last), block, aad)
            block, counter = nxt, counter + 1
