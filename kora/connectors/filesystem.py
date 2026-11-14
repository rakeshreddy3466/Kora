"""Filesystem connector that simulates SharePoint sites / OneDrive / Ericsson Play
libraries for the hackathon. Item metadata a real repository would expose via
its API (owner, ACL, retention label, legal hold) is read from a
`_kora_source.json` manifest at the library root.

Hardening: every path is resolved and must stay inside the root; symlinks are
never followed (O_NOFOLLOW + lstat); writes are atomic.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat as st
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator

from .. import clock
from ..util import CHUNK
from .base import ActionConnector, ConflictError, SourceConnector, SourceItem

MANIFEST = "_kora_source.json"
STUB_SUFFIX = ".kora-stub.json"


def _parse_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None)


class FilesystemConnector(SourceConnector, ActionConnector):
    def __init__(self, root: str, system: str, site: str | None = None):
        self.root = Path(root).resolve()
        self.system = system
        self.site = site
        self._manifest: dict | None = None

    # -- helpers ------------------------------------------------------------
    def _safe(self, rel: str) -> Path:
        if not rel or "\x00" in rel or Path(rel).is_absolute():
            raise ValueError("invalid path")
        p = (self.root / rel)
        resolved = p.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("path escapes source root")
        # reject symlinks anywhere below root
        cur = self.root
        for part in Path(rel).parts:
            cur = cur / part
            if cur.is_symlink():
                raise ValueError("symlinks are not followed")
        return p

    def manifest(self) -> dict:
        if self._manifest is None:
            mp = self.root / MANIFEST
            self._manifest = json.loads(mp.read_text()) if mp.exists() else {}
        return self._manifest

    def _item(self, rel: str, p: Path) -> SourceItem:
        sres = os.lstat(p)
        m = self.manifest()
        meta = (m.get("items") or {}).get(rel, {})
        return SourceItem(
            path=rel, size=sres.st_size,
            modified_at=_parse_dt(meta.get("modified")) or datetime.fromtimestamp(sres.st_mtime, timezone.utc).replace(tzinfo=None),
            created_at=_parse_dt(meta.get("created")) or datetime.fromtimestamp(sres.st_mtime, timezone.utc).replace(tzinfo=None),
            owner=meta.get("owner", m.get("default_owner")),
            acl=list(meta.get("acl", m.get("default_acl", []))),
            retention_label=meta.get("retention_label", m.get("default_retention_label", "standard")),
            legal_hold=bool(meta.get("legal_hold", False)),
            etag=f"{sres.st_mtime_ns}-{sres.st_size}",
            meta={k: v for k, v in meta.items() if k in ("title", "doc_number", "revision", "department")},
        )

    # -- SourceConnector ----------------------------------------------------
    def list_items(self) -> Iterator[SourceItem]:
        self._manifest = None
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for fn in sorted(filenames):
                if fn.startswith(".") or fn == MANIFEST or fn.endswith(STUB_SUFFIX) or fn.endswith(".kora-part"):
                    continue
                p = Path(dirpath) / fn
                if p.is_symlink() or not st.S_ISREG(os.lstat(p).st_mode):
                    continue
                yield self._item(str(p.relative_to(self.root)), p)

    def stat(self, path: str) -> SourceItem | None:
        p = self._safe(path)
        if not p.exists() or p.is_symlink():
            return None
        return self._item(path, p)

    def open(self, path: str) -> BinaryIO:
        p = self._safe(path)
        fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        return os.fdopen(fd, "rb")

    def sidecars(self, path: str) -> list[str]:
        p = Path(path)
        out = []
        for ext in (".vtt", ".srt"):
            cand = str(p.with_suffix(ext))
            try:
                if self._safe(cand).is_file():
                    out.append(cand)
            except ValueError:
                pass
        return out

    # -- ActionConnector ----------------------------------------------------
    def _hash(self, p: Path) -> str:
        h = hashlib.sha256()
        with self.open(str(p.relative_to(self.root))) as fh:
            while chunk := fh.read(CHUNK):
                h.update(chunk)
        return h.hexdigest()

    def _atomic_write(self, dst: Path, data_iter) -> None:
        fd, tmp = tempfile.mkstemp(dir=dst.parent, prefix=".kora-", suffix=".kora-part")
        try:
            with os.fdopen(fd, "wb") as out:
                for chunk in data_iter:
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, dst)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def replace_with_stub(self, path: str, expected_sha: str, stub: dict) -> str:
        p = self._safe(path)
        stub_p = Path(str(p) + STUB_SUFFIX)
        if not p.exists():
            if stub_p.exists() and json.loads(stub_p.read_text()).get("item_id") == stub.get("item_id"):
                return str(stub_p.relative_to(self.root))          # already done (idempotent)
            raise ConflictError(f"{path} no longer exists at source")
        current = self._hash(p)
        if current != expected_sha:
            raise ConflictError(f"{path} changed since plan (sha {current[:12]} != {expected_sha[:12]})")
        body = json.dumps(stub, indent=2, default=str).encode()
        self._atomic_write(stub_p, [body])
        os.unlink(p)
        return str(stub_p.relative_to(self.root))

    def restore(self, path: str, src: BinaryIO, expected_sha: str, stub_path: str | None) -> None:
        p = self._safe(path)
        if p.exists():
            if self._hash(p) == expected_sha:
                pass                                                  # already restored
            else:
                raise ConflictError(f"{path} is occupied by different content; refusing to overwrite")
        else:
            h = hashlib.sha256()

            def gen():
                while chunk := src.read(CHUNK):
                    h.update(chunk)
                    yield chunk
            self._atomic_write(p, gen())
            if h.hexdigest() != expected_sha:
                os.unlink(p)
                raise ConflictError("restored bytes do not match expected hash")
        if stub_path:
            sp = self._safe(stub_path)
            if sp.exists():
                os.unlink(sp)

    def replace_content(self, path: str, expected_sha: str, src: BinaryIO, new_sha: str) -> None:
        p = self._safe(path)
        if not p.exists():
            raise ConflictError(f"{path} no longer exists at source")
        current = self._hash(p)
        if current == new_sha:
            return                                                # already replaced
        if current != expected_sha:
            raise ConflictError(f"{path} changed since plan (sha {current[:12]} != {expected_sha[:12]})")
        h = hashlib.sha256()

        def gen():
            while chunk := src.read(CHUNK):
                h.update(chunk)
                yield chunk
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".kora-", suffix=".kora-part")
        try:
            with os.fdopen(fd, "wb") as out:
                for chunk in gen():
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            if h.hexdigest() != new_sha:
                raise ConflictError("replacement bytes do not match their expected hash")
            os.replace(tmp, p)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def annotate_stub(self, stub_path: str, extra: dict) -> None:
        sp = self._safe(stub_path)
        if sp.exists():
            data = json.loads(sp.read_text())
            data.update(extra)
            data["updated_at"] = clock.now().isoformat()
            self._atomic_write(sp, [json.dumps(data, indent=2, default=str).encode()])
