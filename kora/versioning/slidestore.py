"""Lazy slide store.

Live documents stay in their system of record; KORA keeps only text, hashes
and small thumbnails for them. Single-page PDFs are materialised *only* when
a compaction (superseded revision, excerpt) is being planned, i.e. right
before the original would go away, and only for pages not already stored by
another document. The recovery proof (rebuild + re-render) then runs on
exactly those pages.

Real-data finding behind this: storing every page of every deck eagerly made
the slide store larger than the decks themselves (per-page PDFs repeat fonts
and backgrounds), turning a storage tool into a storage cost.
"""
from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..connectors import get_connector
from ..models import Content, File, SlideBlob, Source
from ..queries import doc_units
from ..security import sandbox
from ..storage import get_blobstore
from ..util import CHUNK


def missing(s: Session, sha: str) -> list:
    return [u for u in doc_units(s, sha) if u.unit_hash and s.get(SlideBlob, u.unit_hash) is None]


def materialize(s: Session, sha: str) -> dict:
    todo = missing(s, sha)
    if not todo:
        return {"stored": 0, "missing": 0}
    c = s.get(Content, sha)
    f = s.scalar(select(File).where(File.sha256 == sha, File.status == "active").limit(1))
    if f is None or c.detected_type not in ("pdf", "pptx"):
        return {"stored": 0, "missing": len(todo), "reason": "no live copy to read pages from"}
    conn = get_connector(s.get(Source, f.source_name))
    store = get_blobstore()
    work = Path(tempfile.mkdtemp(prefix="kora-ss-"))
    try:
        inp = work / f"in.{c.detected_type}"
        h = hashlib.sha256()
        with conn.open(f.path) as fh, open(inp, "wb") as out:
            while chunk := fh.read(CHUNK):
                h.update(chunk)
                out.write(chunk)
        if h.hexdigest() != sha:
            return {"stored": 0, "missing": len(todo), "reason": "source changed"}
        out_dir = work / "out"
        out_dir.mkdir()
        res = sandbox.run(c.detected_type, {"path": str(inp)}, out_dir)
        by_hash = {u["unit_hash"]: u for u in res["units"] if u.get("blob")}
        need, seen = [], set()
        for u in todo:
            r = by_hash.get(u.unit_hash)
            if r is None or u.unit_hash in seen:
                continue
            p = (out_dir / r["blob"]).resolve()
            if p.is_relative_to(out_dir.resolve()) and p.is_file():
                need.append((u.unit_hash, str(p)))
                seen.add(u.unit_hash)
        if not need:
            return {"stored": 0, "missing": len(todo)}
        # one pack per materialisation: fonts and backgrounds are stored once, not once per page
        pack_dir = work / "pack"
        pack_dir.mkdir()
        pk = sandbox.run("pdf_pack", {"pages": [p for _, p in need]}, pack_dir)
        ref = store.put_file("hot", pack_dir / pk["pack"])
        share, stored = ref.size // len(need), 0
        for i, (uh, _) in enumerate(need):
            size = share if i < len(need) - 1 else ref.size - share * (len(need) - 1)
            try:
                with s.begin_nested():
                    s.add(SlideBlob(unit_hash=uh, blob_key=ref.key, page_index=i, size_bytes=size,
                                    fmt="pdf-pack", first_content_sha=sha))
                    s.flush()
                stored += 1
            except IntegrityError:
                pass
        return {"stored": stored, "pack_bytes": ref.size, "missing": len(missing(s, sha))}
    finally:
        shutil.rmtree(work, ignore_errors=True)
