"""Sandbox-side dispatcher. Runs with no database, secrets or network config.

Input : {"task", "args", "out_dir", "options"}
Output: JSON-serialisable dict; binary artifacts are files inside out_dir,
        referenced by relative name.
"""
from __future__ import annotations

from pathlib import Path


class PermanentInput(Exception):
    """Input can never be processed (encrypted, corrupt, over limits)."""


def dispatch(req: dict) -> dict:
    task, args, opts = req["task"], req["args"], req.get("options", {})
    out = Path(req["out_dir"])
    if task == "pdf":
        from . import pdf
        return pdf.extract(Path(args["path"]), out, opts)
    if task == "pptx":
        from . import pptx_
        return pptx_.extract(Path(args["path"]), out, opts)
    if task == "video":
        from . import video
        return video.extract(Path(args["path"]), out, opts, [Path(p) for p in args.get("sidecars", [])])
    if task == "image":
        from . import image
        return image.extract(Path(args["path"]), out, opts)
    if task == "text":
        import hashlib
        from ..fingerprint.text import text_hash
        raw = Path(args["path"]).read_bytes()[:5_000_000].decode("utf-8", errors="replace")
        chunks = [raw[i:i + 4000] for i in range(0, len(raw), 4000)] or [""]
        units = [{"kind": "page", "ordinal": n, "text": t, "text_hash": text_hash(t),
                  "unit_hash": hashlib.sha256(b"text|" + t.encode()).hexdigest(), "body_hash": None,
                  "phash": None, "thumb": None, "blob": None, "meta": {}} for n, t in enumerate(chunks, 1)]
        return {"type": "text", "metadata": {}, "units": units,
                "normalized_hash": hashlib.sha256("|".join(u["unit_hash"] for u in units).encode()).hexdigest()}
    if task == "docx":
        from . import office
        return office.docx(Path(args["path"]))
    if task == "msg":
        from . import office
        return office.msg(Path(args["path"]))
    if task == "slide_spec":
        from . import slidespec
        return slidespec.run(Path(args["path"]), out)
    if task == "optimize_video":
        from . import optimize
        return optimize.video(Path(args["path"]), out, args)
    if task == "optimize_pdf":
        from . import optimize
        return optimize.pdf(Path(args["path"]), out, args)
    if task == "pdf_pack":
        import pymupdf
        pack = pymupdf.open()
        for pth in args["pages"]:
            with pymupdf.open(pth) as one:
                pack.insert_pdf(one)
        dst = out / "pack.pdf"
        try:
            pack.subset_fonts()
        except Exception:
            pass
        pack.save(dst, garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, clean=True, use_objstms=1)
        return {"pack": dst.name, "pages": pack.page_count}
    if task == "pdf_render_hashes":
        from . import pdf
        return pdf.render_hashes(Path(args["path"]))
    raise PermanentInput(f"unknown task {task}")
