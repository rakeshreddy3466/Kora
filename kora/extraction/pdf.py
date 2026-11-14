"""PDF -> per-page units.

For every page we compute
  text_hash   sha256(normalised text)              - textual identity
  render_hash sha256(72-dpi greyscale raster)       - exact visual identity
  unit_hash   sha256(render_hash | text_hash)       - THE slide identifier
  phash       64-bit perceptual hash                - fuzzy visual identity
and write a single-page PDF (for the slide store), a thumbnail, and the hashes
of embedded images. Same page in two decks -> same unit_hash, regardless of
file name, document metadata or position in the deck.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image

from ..fingerprint.phash import phash
from ..fingerprint.text import text_hash
from .entry import PermanentInput

RENDER_ZOOM = 1.0       # 72 dpi: enough for identity, cheap to compute
FOOTER_BAND = 0.08      # bottom band excluded from body identity (page numbers, doc-number footers)
THUMB_ZOOM = 0.3       # search/UI preview only; small on purpose
MAX_IMAGES_PER_PAGE = 50


def _open(path: Path):
    import pymupdf
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        raise PermanentInput(f"unreadable PDF: {e}") from e
    if doc.needs_pass:
        raise PermanentInput("password-protected PDF")
    if doc.page_count == 0:
        raise PermanentInput("PDF has no readable pages")
    return doc


def page_identity(page) -> tuple[str, str, str, str]:
    import pymupdf
    text = page.get_text("text")
    pix = page.get_pixmap(matrix=pymupdf.Matrix(RENDER_ZOOM, RENDER_ZOOM), colorspace=pymupdf.csGRAY, alpha=False)
    rh = hashlib.sha256(f"{pix.width}x{pix.height}|".encode() + pix.samples).hexdigest()
    th = text_hash(text)
    uh = hashlib.sha256(f"pdfpage|{rh}|{th}".encode()).hexdigest()
    ph = phash(Image.frombytes("L", (pix.width, pix.height), pix.samples))
    return text, th, uh, ph


def body_identity(page) -> tuple[str, str]:
    """Footer-insensitive identity: render + text of the page minus the bottom band."""
    import pymupdf
    r = page.rect
    clip = pymupdf.Rect(r.x0, r.y0, r.x1, r.y1 - r.height * FOOTER_BAND)
    pix = page.get_pixmap(matrix=pymupdf.Matrix(RENDER_ZOOM, RENDER_ZOOM), colorspace=pymupdf.csGRAY,
                          alpha=False, clip=clip)
    body_text = "\n".join(b[4] for b in page.get_text("blocks") if b[3] <= clip.y1 + 0.5)
    bh = hashlib.sha256(b"body|" + pix.samples + b"|" + text_hash(body_text).encode()).hexdigest()
    return body_text, bh


def extract(path: Path, out: Path, opts: dict) -> dict:
    import pymupdf
    doc = _open(path)
    if doc.page_count > int(opts.get("max_pages", 3000)):
        raise PermanentInput(f"{doc.page_count} pages exceeds limit")
    units = []
    for i, page in enumerate(doc):
        n = i + 1
        text, th, uh, ph = page_identity(page)
        body_text, bh = body_identity(page)
        tpix = page.get_pixmap(matrix=pymupdf.Matrix(THUMB_ZOOM, THUMB_ZOOM), alpha=False)
        thumb = out / f"thumb-{n:04d}.jpg"
        Image.frombytes("RGB", (tpix.width, tpix.height), tpix.samples).save(thumb, "JPEG", quality=60)
        one = pymupdf.open()
        one.insert_pdf(doc, from_page=i, to_page=i)
        one.set_metadata({})
        page_pdf = out / f"page-{n:04d}.pdf"
        one.save(page_pdf, garbage=4, deflate=True, no_new_id=True)
        one.close()
        images = []
        for img in page.get_images(full=True)[:MAX_IMAGES_PER_PAGE]:
            try:
                images.append(hashlib.sha256(doc.extract_image(img[0])["image"]).hexdigest())
            except Exception:
                continue
        units.append({"kind": "page", "ordinal": n, "text": text, "text_hash": th, "unit_hash": uh,
                      "body_hash": bh, "body_text": body_text, "phash": ph, "thumb": thumb.name, "blob": page_pdf.name,
                      "meta": {"width": round(page.rect.width), "height": round(page.rect.height),
                               "images": sorted(set(images))}})
    meta = {k: v for k, v in (doc.metadata or {}).items() if v}
    try:
        meta["toc"] = [t[1] for t in doc.get_toc()][:200]
    except Exception:
        pass
    doc.close()
    normalized = hashlib.sha256("|".join(u["unit_hash"] for u in units).encode()).hexdigest()
    return {"type": "pdf", "metadata": meta, "units": units, "normalized_hash": normalized}


def render_hashes(path: Path) -> dict:
    """Re-derive unit hashes of a (reconstructed) PDF - used to *prove* recoverability."""
    doc = _open(path)
    hashes = [page_identity(p)[2] for p in doc]
    doc.close()
    return {"unit_hashes": hashes}
