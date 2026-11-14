"""Sandbox side of the compression engine: produce a smaller rendition AND measure it.

video  H.264 (broad playback on SharePoint/Teams/Play), CRF-based, frame rate capped
       for screen content, mono AAC speech audio, +faststart.
       Measured: duration, audio present, SSIM vs original (1 fps sample) and
       *slide readability*: OCR of the rendition at each keyframe time must recover
       the words OCR found in the original.
pdf    embedded images re-sampled to a target dpi and JPEG quality; object streams.
       Measured: page count, per-page text identical, per-page render SSIM.
Nothing here decides whether the rendition is acceptable - the parent compares the
measurements with the policy's quality budget.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import numpy as np

from .entry import PermanentInput

FF = ["-nostdin", "-hide_banner", "-protocol_whitelist", "file"]


def _ssim(a: np.ndarray, b: np.ndarray) -> float:
    """Global SSIM of two grey images (same shape), constants per Wang et al."""
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    ma, mb = a.mean(), b.mean()
    va, vb = a.var(), b.var()
    cov = ((a - ma) * (b - mb)).mean()
    return float(((2 * ma * mb + c1) * (2 * cov + c2)) / ((ma ** 2 + mb ** 2 + c1) * (va + vb + c2)))


def _tok(s: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{3,}", (s or "").lower())}


def _ffmpeg_with_progress(cmd: list[str], out: Path, step: str, total: int | None,
                          timeout_s: int = 7200) -> tuple[int, str]:
    """Run ffmpeg and report how far it got (seconds of media done) for the dashboard."""
    import tempfile
    import time
    from .progress import report
    report(out, step, 0 if total else None, total, "s")
    with tempfile.TemporaryFile(dir=out) as errf:
        p = subprocess.Popen(["ffmpeg", *FF, "-progress", "pipe:1", "-nostats", *cmd],
                             stdout=subprocess.PIPE, stderr=errf, text=True)
        start = time.monotonic()
        for line in p.stdout:                                   # key=value lines, a block every ~0.5 s
            if line.startswith("out_time_us=") and total:
                try:
                    done = int(line.split("=", 1)[1]) // 1_000_000
                    report(out, step, max(0, min(done, total)), total, "s")
                except ValueError:
                    pass
            if time.monotonic() - start > timeout_s:
                p.kill()
                break
        rc = p.wait()
        errf.seek(0)
        return rc, errf.read().decode(errors="replace")


def video(path: Path, out: Path, args: dict) -> dict:
    from .progress import report
    from .video import probe
    info = probe(path)
    h = min(info["height"], int(args.get("max_height", 1080)))
    fps = min(info["fps"] or 25, float(args.get("max_fps", 15)))
    dst = out / "rendition.mp4"
    total = int(info["duration"] or 0) or None
    rc, err = _ffmpeg_with_progress(
        ["-i", str(path), "-map", "0:v:0", "-map", "0:a:0?",
         "-vf", f"fps={fps},scale=-2:{h}", "-c:v", "libx264", "-preset", args.get("preset", "veryfast"),
         "-crf", str(args.get("crf", 28)), "-tune", "stillimage", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", f"{int(args.get('audio_kbps', 64))}k", "-ac", "1",
         "-movflags", "+faststart", "-y", str(dst)], out, "transcoding to a smaller version", total)
    if rc != 0 or not dst.exists():
        raise PermanentInput(f"transcode failed: {err[-300:]}")
    ri = probe(dst)
    rc, err = _ffmpeg_with_progress(
        ["-i", str(dst), "-i", str(path), "-lavfi",
         "[0:v]fps=1,scale=640:360,setsar=1[a];[1:v]fps=1,scale=640:360,setsar=1[b];[a][b]ssim",
         "-f", "null", "-"], out, "comparing picture quality, before and after", total)
    m = re.search(r"All:([0-9.]+)", err)
    ssim = float(m.group(1)) if m else 0.0
    recall = []
    checks = [(t, text) for t, text in args.get("ocr_frames", [])[:12] if len(_tok(text)) >= 5]
    for i, (t, text) in enumerate(checks):
        report(out, "checking slide text is still readable (OCR)", i, len(checks))
        want = _tok(text)
        img = out / f"ocr-{t:.0f}.png"
        subprocess.run(["ffmpeg", *FF, "-ss", f"{t + 1.0:.2f}", "-i", str(dst), "-frames:v", "1", "-y", str(img)],
                       capture_output=True, timeout=120)
        if not img.exists():
            continue
        o = subprocess.run(["tesseract", str(img), "stdout", "--psm", "3", "-l", "eng"], capture_output=True, timeout=120)
        got = _tok(o.stdout.decode(errors="replace"))
        recall.append(len(want & got) / len(want))
    # before/after evidence for the approver: the same instant from both files
    times = sorted(args.get("ocr_frames", []), key=lambda x: -len(_tok(x[1])))[:2]
    times = [t + 1.0 for t, _ in times] or [info["duration"] * 0.3, info["duration"] * 0.6]
    previews = []
    for i, t in enumerate(sorted(times)):
        pair = {}
        for side, src in (("before", path), ("after", dst)):
            img = out / f"preview-{i}-{side}.jpg"
            subprocess.run(["ffmpeg", *FF, "-ss", f"{t:.2f}", "-i", str(src), "-frames:v", "1",
                            "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", "-y", str(img)],
                           capture_output=True, timeout=120)
            if img.exists():
                pair[side] = img.name
        if len(pair) == 2:
            previews.append({"label": f"frame at {int(t // 60)}:{int(t % 60):02d}", **pair})
    return {"file": dst.name, "bytes": dst.stat().st_size, "previews": previews,
            "measured": {"duration_delta_s": round(abs(ri["duration"] - info["duration"]), 3),
                         "has_audio": ri["has_audio"], "height": ri["height"], "fps": ri["fps"],
                         "bit_rate": ri["bit_rate"], "ssim": round(ssim, 4),
                         "ocr_recall": round(float(np.mean(recall)), 3) if recall else None,
                         "ocr_frames_checked": len(recall),
                         "settings": {"crf": args.get("crf", 28), "max_fps": fps, "height": h}}}


def _box(x: np.ndarray, w: int) -> np.ndarray:
    """Mean over every w x w window (valid positions) via an integral image."""
    c = np.pad(x, ((1, 0), (1, 0))).cumsum(0).cumsum(1)
    return (c[w:, w:] - c[:-w, w:] - c[w:, :-w] + c[:-w, :-w]) / (w * w)


def mssim(a: np.ndarray, b: np.ndarray, w: int = 7) -> float:
    """Mean local SSIM (Wang et al. 2004, uniform 7x7 window) of two grey images."""
    a = a.astype(np.float64)
    b = b.astype(np.float64)
    if min(a.shape) < w:
        return _ssim(a, b)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    ma, mb = _box(a, w), _box(b, w)
    va, vb = _box(a * a, w) - ma ** 2, _box(b * b, w) - mb ** 2
    cov = _box(a * b, w) - ma * mb
    m = ((2 * ma * mb + c1) * (2 * cov + c2)) / ((ma ** 2 + mb ** 2 + c1) * (va + vb + c2))
    return float(m.mean())


def _gray(page, zoom: float) -> np.ndarray:
    import pymupdf
    x = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csGRAY, alpha=False)
    return np.frombuffer(x.samples, dtype=np.uint8).reshape(x.height, x.width)


def _pdf_images(d) -> dict[int, dict]:
    """xref -> {pages, dpi (highest effective dpi over all placements), smask}."""
    imgs: dict[int, dict] = {}
    for pno in range(d.page_count):
        page = d[pno]
        for img in page.get_images(full=True):
            xref, smask, w = img[0], img[1], img[2]
            e = imgs.setdefault(xref, {"pages": set(), "dpi": 0.0, "smask": bool(smask), "width": w})
            e["pages"].add(pno)
            try:
                for r in page.get_image_rects(xref):
                    if r.width > 0:
                        e["dpi"] = max(e["dpi"], w / (r.width / 72.0))
            except Exception:
                e["dpi"] = 0.0                                  # unknown placement: leave alone
    return imgs


def _recompress(src: Path, dst: Path, plan: dict[int, dict], quality: int, target: float, skip: set[int]) -> dict:
    """Re-encode the planned images (downsample to target dpi, JPEG quality q); keep the rest byte-identical."""
    import io

    import pymupdf
    from PIL import Image
    done = {}
    with pymupdf.open(src) as d:
        for xref, e in plan.items():
            if xref in skip:
                continue
            try:
                raw = d.xref_stream_raw(xref)
                if d.xref_get_key(xref, "Decode")[0] != "null" or d.xref_get_key(xref, "ImageMask")[1] == "true":
                    continue                                    # inverted / mask images: never touch
                info = d.extract_image(xref)
                im = Image.open(io.BytesIO(info["image"]))
                im.load()
            except Exception:
                continue
            comps = 3 if im.mode == "RGB" else 1 if im.mode == "L" else 0
            if not comps or info.get("colorspace") != comps:
                continue                                        # CMYK, palette, alpha: never touch
            scale = min(1.0, target / e["dpi"]) if e["dpi"] else 1.0
            if scale < 0.999:
                im = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=quality, optimize=True, progressive=True, subsampling="4:2:0")
            new = buf.getvalue()
            if len(new) >= 0.9 * len(raw):
                continue                                        # not worth a generation loss
            # rewrite the image XObject in place: same xref, same colour space, new DCT stream
            d.update_stream(xref, new, compress=False)
            d.xref_set_key(xref, "Filter", "/DCTDecode")
            d.xref_set_key(xref, "DecodeParms", "null")
            d.xref_set_key(xref, "Width", str(im.width))
            d.xref_set_key(xref, "Height", str(im.height))
            d.xref_set_key(xref, "BitsPerComponent", "8")
            d.xref_set_key(xref, "Length", str(len(new)))
            done[xref] = {"from": len(raw), "to": len(new)}
        d.save(dst, garbage=3, deflate=True, use_objstms=1)
    return done


def pdf(path: Path, out: Path, args: dict) -> dict:
    """Per-image recompression with a measured, per-page fallback: any page whose render drifts
    below the budget gets its images restored to the original bytes, and the PDF is rebuilt."""
    import pymupdf
    dst = out / "rendition.pdf"
    quality = int(args.get("quality", 80))
    target = float(args.get("dpi_target", 150))
    threshold = float(args.get("dpi_threshold", 200))
    zoom = float(args.get("measure_zoom", 2.0))                 # 144 dpi: stricter than on-screen viewing
    floor = float(args.get("min_page_ssim", 0.97))
    with pymupdf.open(path) as d:
        if d.needs_pass:
            raise PermanentInput("password-protected PDF")
        imgs = _pdf_images(d)
    plan = {x: e for x, e in imgs.items() if not e["smask"] and e["dpi"] > threshold}
    skip: set[int] = set()
    rounds, done, scores, text_same, pages_equal = 0, {}, [], True, True
    while True:
        rounds += 1
        done = _recompress(path, dst, plan, quality, target, skip)
        scores, text_same = [], True
        with pymupdf.open(path) as a, pymupdf.open(dst) as b:
            pages_equal = a.page_count == b.page_count
            for pa, pb in zip(a, b):
                text_same &= pa.get_text("text") == pb.get_text("text")
                ia, ib = _gray(pa, zoom), _gray(pb, zoom)
                scores.append(mssim(ia, ib) if ia.shape == ib.shape else 0.0)
        bad = {i for i, sc in enumerate(scores) if sc < floor}
        revert = {x for x, e in plan.items() if x in done and e["pages"] & bad}
        if not revert or rounds >= 3:
            break
        skip |= revert                                          # keep those images byte-identical, try again
    previews = []
    if scores:
        worst = int(np.argmin(scores))
        with pymupdf.open(path) as a, pymupdf.open(dst) as b:
            for side, doc in (("before", a), ("after", b)):
                pix = doc[worst].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5), alpha=False)
                pix.save(str(out / f"preview-0-{side}.jpg"), jpg_quality=85)
        previews.append({"label": f"worst page: {worst + 1} (SSIM {scores[worst]:.3f})",
                         "before": "preview-0-before.jpg", "after": "preview-0-after.jpg"})
    return {"file": dst.name, "bytes": dst.stat().st_size, "previews": previews,
            "measured": {"pages_equal": pages_equal, "text_identical": text_same,
                         "min_page_ssim": round(min(scores) if scores else 1.0, 4),
                         "pages_below_floor": [i + 1 for i, sc in enumerate(scores) if sc < floor],
                         "images_total": len(imgs), "images_recompressed": len(done),
                         "images_kept_after_fallback": len(skip), "rounds": rounds,
                         "metric": f"mean 7x7 SSIM at {int(72 * zoom)} dpi",
                         "settings": {"dpi_target": target, "dpi_threshold": threshold, "quality": quality}}}
