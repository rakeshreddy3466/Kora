"""Compression decision engine with a quality budget (parent side).

For each content type the engine picks an operation, the sandbox produces the
rendition and *measures* it, and this module judges the measurements against
the policy's quality budget. Only a rendition that passes every budget check
AND saves at least `min_saving_ratio` can be offered to the governance layer,
where replacing the original is a normal plan (approval, quarantine of the
original, restore, purge). Engineering diagrams are never compressed lossily.

  video.transcode   budget: duration, audio kept, SSIM >= 0.95, slide text still
                    OCR-readable (recall >= 0.9 of the original's words)
  pdf.recompress    budget: same pages, identical text, every page mean-SSIM >= 0.97 at 144 dpi;
                    images on a page that drifts are restored byte-identical and the PDF rebuilt
"""
from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import db
from ..connectors import get_connector
from ..governance.policy import get_policy
from ..models import Content, File, Rendition, Source, Unit
from ..security import sandbox
from ..storage import get_blobstore
from ..util import CHUNK

DIAGRAM_HINTS = ("schematic", "wiring", "drawing no", "circuit", "floor plan", "engineering drawing")
OPS = {"video": "video.transcode", "pdf": "pdf.recompress"}


def content_class(c: Content) -> str:
    text = " ".join(str(v) for v in (c.meta or {}).values()).lower()
    return "engineering_diagram" if any(h in text for h in DIAGRAM_HINTS) else (c.detected_type or "unknown")


def candidates(c: Content) -> list[str]:
    if c.detected_type not in OPS or content_class(c) == "engineering_diagram":
        return []
    if (c.meta or {}).get("rendition_of"):                  # never re-encode a rendition
        return []
    if c.detected_type == "video":
        m = c.media or {}
        if (m.get("bit_rate") or 0) < 400_000:              # already lean
            return []
    if c.size_bytes < 200_000:                               # not worth a job
        return []
    return [OPS[c.detected_type]]


def judge(op: str, measured: dict, original: int, rendition: int, had_audio: bool = True) -> tuple[bool, list[dict]]:
    cp = get_policy().get("compression", {})
    checks = []

    def ck(name, ok, detail):
        checks.append({"name": name, "passed": bool(ok), "detail": detail})
    saving = 1 - rendition / original if original else 0
    ck("net_saving", saving >= cp.get("min_saving_ratio", 0.2),
       f"{saving:.1%} smaller (policy >= {cp.get('min_saving_ratio', 0.2):.0%})")
    if op == "video.transcode":
        vb = cp.get("video", {})
        ck("duration", measured["duration_delta_s"] <= vb.get("max_duration_delta_s", 1.0),
           f"duration differs by {measured['duration_delta_s']} s")
        ck("audio_kept", measured["has_audio"] or not had_audio, "speech track present")
        ck("ssim", measured["ssim"] >= vb.get("min_ssim", 0.95), f"SSIM {measured['ssim']} (>= {vb.get('min_ssim', 0.95)})")
        if measured.get("ocr_frames_checked"):
            ck("slide_text_readable", measured["ocr_recall"] >= vb.get("min_ocr_recall", 0.9),
               f"OCR recovers {measured['ocr_recall']:.0%} of slide words on {measured['ocr_frames_checked']} keyframes")
    elif op == "pdf.recompress":
        pb = cp.get("pdf", {})
        ck("pages", measured["pages_equal"], "same page count")
        ck("text_identical", measured["text_identical"], "every page's text unchanged")
        ck("render_ssim", measured["min_page_ssim"] >= pb.get("min_page_ssim", 0.97),
           f"worst page SSIM {measured['min_page_ssim']} (>= {pb.get('min_page_ssim', 0.97)}; "
           f"{measured.get('metric', 'global SSIM')})")
    return all(c["passed"] for c in checks), checks


def run(sha: str, op: str, on_work_dir=None) -> Rendition | None:
    """Produce, measure and judge one rendition. Heavy work happens outside any DB transaction."""
    cp = get_policy().get("compression", {})
    with db.session_scope() as s:
        c = s.get(Content, sha)
        f = s.scalar(select(File).where(File.sha256 == sha, File.status == "active").limit(1))
        if c is None or f is None:
            return None
        src = s.get(Source, f.source_name)
        s.expunge(src)
        path, ext, size, had_audio = f.path, f.extension or "bin", c.size_bytes, bool((c.media or {}).get("has_audio"))
        ocr_frames = [(u.start_s or 0.0, u.text or "") for u in s.scalars(
            select(Unit).where(Unit.content_sha == sha, Unit.kind == "frame").order_by(Unit.ordinal))]
    work = Path(tempfile.mkdtemp(prefix="kora-opt-"))
    try:
        inp = work / f"in.{ext}"
        h = hashlib.sha256()
        with get_connector(src).open(path) as fh, open(inp, "wb") as out:
            while chunk := fh.read(CHUNK):
                h.update(chunk)
                out.write(chunk)
        if h.hexdigest() != sha:
            return None                                       # source changed; a rescan will re-hash
        out_dir = work / "out"
        out_dir.mkdir()
        if on_work_dir:
            on_work_dir(out_dir)                              # lets the job show the sandbox's progress
        if op == "video.transcode":
            res = sandbox.run("optimize_video", {"path": str(inp), "ocr_frames": ocr_frames, **cp.get("video", {})}, out_dir)
        else:
            res = sandbox.run("optimize_pdf", {"path": str(inp), **cp.get("pdf", {})}, out_dir)
        rpath = (out_dir / res["file"]).resolve()
        if not rpath.is_relative_to(out_dir.resolve()):
            raise ValueError("invalid rendition path")
        passed, checks = judge(op, res["measured"], size, res["bytes"], had_audio)
        store = get_blobstore()
        key = store.put_file("hot", rpath).key if passed else None
        previews = []
        for pv in res.get("previews", [])[:4]:                    # evidence for the approver, pass or fail
            pair = {"label": str(pv.get("label", ""))[:80]}
            for side in ("before", "after"):
                pp = (out_dir / str(pv.get(side, ""))).resolve()
                if pp.is_relative_to(out_dir.resolve()) and pp.is_file() and pp.stat().st_size < 5_000_000:
                    pair[side] = store.put_file("hot", pp).key
            if "before" in pair and "after" in pair:
                previews.append(pair)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    with db.session_scope() as s:
        r = Rendition(content_sha=sha, operation=op, blob_key=key, size_bytes=res["bytes"],
                      validation={"measured": res["measured"], "checks": checks, "original_bytes": size,
                                  "previews": previews,
                                  "saving_ratio": round(1 - res["bytes"] / size, 4) if size else 0},
                      passed=passed)
        s.add(r)
        s.flush()
        s.expunge(r)
        return r


def best(s: Session, sha: str) -> Rendition | None:
    rs = [r for r in s.scalars(select(Rendition).where(Rendition.content_sha == sha, Rendition.passed.is_(True)))
          if r.blob_key]
    return min(rs, key=lambda r: r.size_bytes) if rs else None
