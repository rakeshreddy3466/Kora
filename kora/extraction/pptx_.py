"""PPTX -> per-slide units.

Without a renderer, a slide's identity is structural: normalised text +
embedded media hashes + layout + rounded shape geometry. That is stable across
re-saves and independent of the file it lives in. With
KORA_PPTX_RENDERER=libreoffice the deck is also rendered to PDF so slides get
render-based unit hashes, perceptual hashes (needed for matching video frames)
and slide-store pages (needed for render-equivalent recovery).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

from ..fingerprint.text import normalize, text_hash
from .entry import PermanentInput

EMU_PER_PT = 12700
FOOTER_PLACEHOLDERS = {13, 15, 16}     # SLIDE_NUMBER, FOOTER, DATE


def _walk(shapes):
    for sh in shapes:
        yield sh
        if sh.shape_type == 6 and hasattr(sh, "shapes"):   # MSO_SHAPE_TYPE.GROUP
            yield from _walk(sh.shapes)


def _unhidden_copy(path: Path, out: Path) -> Path:
    """Renderers skip hidden slides; a recovery proof must cover every slide, so render a
    temporary copy with the hidden flag cleared (content untouched)."""
    from pptx import Presentation
    prs = Presentation(str(path))
    changed = False
    for sl in prs.slides:
        if sl._element.get("show") == "0":
            del sl._element.attrib["show"]
            changed = True
    if not changed:
        return path
    dst = out / ("render-src" + path.suffix)
    prs.save(str(dst))
    return dst


def _render_pdf(path: Path, out: Path) -> Path | None:
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    mac = Path("/Applications/LibreOffice.app/Contents/MacOS/soffice")
    if not exe and mac.exists():
        exe = str(mac)
    if not exe:
        return None
    try:
        path = _unhidden_copy(path, out)
    except Exception:
        pass
    try:
        subprocess.run([exe, "--headless", "--norestore", f"-env:UserInstallation=file://{out}/lo-profile",
                        "--convert-to", "pdf", "--outdir", str(out / "render"), str(path)],
                       check=True, timeout=300, capture_output=True)
    except Exception:
        return None
    pdfs = list((out / "render").glob("*.pdf"))
    return pdfs[0] if pdfs else None


def extract(path: Path, out: Path, opts: dict) -> dict:
    from pptx import Presentation
    try:
        prs = Presentation(str(path))
    except Exception as e:
        raise PermanentInput(f"unreadable PPTX: {e}") from e
    cp = prs.core_properties
    meta = {"title": cp.title, "author": cp.author, "subject": cp.subject, "keywords": cp.keywords,
            "category": cp.category, "identifier": cp.identifier, "version": cp.version,
            "content_status": cp.content_status, "last_modified_by": cp.last_modified_by,
            "save_count": cp.revision,
            "created": cp.created.isoformat() if cp.created else None,
            "modified": cp.modified.isoformat() if cp.modified else None}
    meta = {k: v for k, v in meta.items() if v not in (None, "")}
    from .office import custom_props, ericsson_meta
    meta.update(ericsson_meta(custom_props(path)))
    units = []
    for i, slide in enumerate(prs.slides, start=1):
        texts, media, geom, body_texts = [], [], [], []
        for sh in _walk(slide.shapes):
            is_footer = False
            if getattr(sh, "is_placeholder", False) and sh.is_placeholder:
                try:
                    is_footer = int(sh.placeholder_format.type) in FOOTER_PLACEHOLDERS
                except Exception:
                    is_footer = False
            if getattr(sh, "has_text_frame", False) and sh.has_text_frame and not is_footer:
                body_texts.append(sh.text_frame.text)
            if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
                texts.append(sh.text_frame.text)
            if getattr(sh, "has_table", False) and sh.has_table:
                for row in sh.table.rows:
                    texts.append(" | ".join(c.text for c in row.cells))
            if sh.shape_type == 13:                      # PICTURE
                try:
                    media.append(hashlib.sha256(sh.image.blob).hexdigest())
                except Exception:
                    pass
            geom.append([int(sh.shape_type or 0), int((sh.left or 0) // EMU_PER_PT // 4),
                         int((sh.top or 0) // EMU_PER_PT // 4), int((sh.width or 0) // EMU_PER_PT // 4),
                         int((sh.height or 0) // EMU_PER_PT // 4)])
        notes = ""
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text
        text = "\n".join(t for t in texts if t)
        struct = {"layout": slide.slide_layout.name, "text": normalize(text), "media": sorted(media),
                  "geom": sorted(geom)}
        uh = hashlib.sha256(b"pptxslide|" + json.dumps(struct, sort_keys=True).encode()).hexdigest()
        body = dict(struct, text=normalize("\n".join(t for t in body_texts if t)))
        bh = hashlib.sha256(b"pptxbody|" + json.dumps(body, sort_keys=True).encode()).hexdigest()
        units.append({"kind": "slide", "ordinal": i, "text": text, "text_hash": text_hash(text),
                      "unit_hash": uh, "body_hash": bh, "body_text": "\n".join(t for t in body_texts if t), "phash": None, "thumb": None, "blob": None,
                      "meta": {"notes": notes[:5000], "layout": slide.slide_layout.name, "images": sorted(set(media)),
                               "hidden": slide._element.get("show") == "0"}})

    rendered = False
    if opts.get("pptx_renderer") == "libreoffice" and units:
        pdf_path = _render_pdf(path, out)
        if pdf_path:
            from . import pdf as pdfx
            r = pdfx.extract(pdf_path, out, opts)
            targets = units if len(r["units"]) == len(units) else [u for u in units if not u["meta"].get("hidden")]
            if len(r["units"]) == len(targets):
                for u, ru in zip(targets, r["units"]):
                    u["unit_hash"] = hashlib.sha256(f"pptxrender|{ru['unit_hash']}".encode()).hexdigest()
                    u["body_hash"] = hashlib.sha256(f"pptxrender|{ru['body_hash']}".encode()).hexdigest()
                    u["phash"], u["thumb"], u["blob"] = ru["phash"], ru["thumb"], ru["blob"]
                    u["meta"]["page_unit_hash"] = ru["unit_hash"]
                rendered = True
    normalized = hashlib.sha256("|".join(u["unit_hash"] for u in units).encode()).hexdigest()
    return {"type": "pptx", "metadata": meta, "units": units, "normalized_hash": normalized, "rendered": rendered}
