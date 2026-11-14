"""Render-equivalent reconstruction of a document from the slide store, and
the *proof* that it works: rebuild, re-render in the sandbox, compare every
page's unit hash with the hash recorded at extraction time."""
from __future__ import annotations

import tempfile
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Content, SlideBlob
from ..queries import doc_units
from ..security import sandbox
from ..storage import get_blobstore


class NotReconstructable(Exception):
    pass


def reconstruct_pdf(s: Session, sha: str) -> bytes:
    import pymupdf
    store = get_blobstore()
    units = doc_units(s, sha)
    if not units:
        raise NotReconstructable("no page units")
    out = pymupdf.open()
    cache: dict[str, object] = {}
    for u in units:
        sb = s.get(SlideBlob, u.unit_hash) if u.unit_hash else None
        if sb is None:
            raise NotReconstructable(f"slide {u.ordinal} not in slide store")
        if sb.blob_key not in cache:
            cache[sb.blob_key] = pymupdf.open(stream=store.get_bytes("hot", sb.blob_key), filetype="pdf")
        out.insert_pdf(cache[sb.blob_key], from_page=sb.page_index or 0, to_page=sb.page_index or 0)
    for d in cache.values():
        d.close()
    c = s.get(Content, sha)
    meta = c.meta or {}
    out.set_metadata({k: str(meta[k]) for k in ("title", "author", "subject", "keywords") if meta.get(k)})
    data = out.tobytes(garbage=3, deflate=True)
    out.close()
    return data


def verify(s: Session, sha: str) -> dict:
    units = doc_units(s, sha)
    missing = [u.ordinal for u in units if not u.unit_hash or s.get(SlideBlob, u.unit_hash) is None]
    if not units or missing:
        return {"ok": False, "reason": "slides missing from slide store", "missing": missing, "pages": len(units)}
    try:
        data = reconstruct_pdf(s, sha)
    except Exception as e:
        return {"ok": False, "reason": f"reconstruction failed: {e}", "pages": len(units)}
    with tempfile.TemporaryDirectory(prefix="kora-verify-") as td:
        p = Path(td) / "rebuilt.pdf"
        p.write_bytes(data)
        res = sandbox.run("pdf_render_hashes", {"path": str(p)}, Path(td))
    expected = [u.meta.get("page_unit_hash", u.unit_hash) for u in units]
    got = res["unit_hashes"]
    mism = [i + 1 for i, (a, b) in enumerate(zip(expected, got)) if a != b]
    ok = len(expected) == len(got) and not mism
    return {"ok": ok, "pages": len(units), "mismatched_pages": mism, "rebuilt_bytes": len(data),
            "method": "rebuild from slide store + re-render + per-page unit hash comparison"}
