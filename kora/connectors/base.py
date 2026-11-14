"""Connector contracts.

Reads and writes are deliberately separate interfaces with separate
credentials: the ingestion pipeline only ever holds a read-only
SourceConnector. Only the deletion executor obtains an ActionConnector, and
its operations are narrow, conditional (expected hash / eTag) and idempotent.
For SharePoint/OneDrive these map to Graph API calls with If-Match eTags;
for Ericsson Play to its media API.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import BinaryIO, Iterator


class ConflictError(Exception):
    """The item changed since the plan was evidenced (TOCTOU protection)."""


@dataclass
class SourceItem:
    path: str
    size: int
    modified_at: datetime | None
    created_at: datetime | None
    owner: str | None = None
    acl: list[str] = field(default_factory=list)
    retention_label: str = "standard"
    legal_hold: bool = False
    etag: str | None = None
    meta: dict = field(default_factory=dict)


class SourceConnector(ABC):
    system: str
    site: str | None

    @abstractmethod
    def list_items(self) -> Iterator[SourceItem]: ...

    @abstractmethod
    def stat(self, path: str) -> SourceItem | None: ...

    @abstractmethod
    def open(self, path: str) -> BinaryIO: ...

    def sidecars(self, path: str) -> list[str]:
        """Related items (e.g. a .vtt transcript next to a recording)."""
        return []


class ActionConnector(ABC):
    @abstractmethod
    def replace_with_stub(self, path: str, expected_sha: str, stub: dict) -> str:
        """Remove the item iff its current content hash == expected_sha, leaving a
        pointer stub. Returns stub path. Idempotent. Raises ConflictError."""

    @abstractmethod
    def restore(self, path: str, src: BinaryIO, expected_sha: str, stub_path: str | None) -> None:
        """Write bytes back to path (iff path is free or already identical) and remove the stub."""

    @abstractmethod
    def annotate_stub(self, stub_path: str, extra: dict) -> None: ...

    @abstractmethod
    def replace_content(self, path: str, expected_sha: str, src: BinaryIO, new_sha: str) -> None:
        """Atomically replace the bytes at path iff they still hash to expected_sha. Idempotent."""
