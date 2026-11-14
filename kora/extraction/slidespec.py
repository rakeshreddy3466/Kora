"""Sandbox side: describe a slide frame as text (layout + words) and recreate it from that text.

Brief use case: "a frame of a slide in a video ... described in a text prompt so the slide can
be recreated". The description is a small JSON spec (canvas, background, text blocks with
position/size/colour, picture regions) plus a plain-language prompt. The recreation is
rendered from the spec alone, then OCR-compared with the original frame, so the claim
"recreatable from text" comes with a measured text recall and the byte cost of each form.
"""
from __future__ import annotations

import json
import subprocess
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
         "/System/Library/Fonts/Supplemental/Arial.ttf", "/Library/Fonts/Arial.ttf", "C:/Windows/Fonts/arial.ttf"]
BOLD = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"]


def _font(size: int, bold: bool = False):
    for f in (BOLD if bold else FONTS):
        if Path(f).is_file():
            return ImageFont.truetype(f, size)
    return ImageFont.load_default(size=size)


def _hex(rgb) -> str:
    return "#{:02x}{:02x}{:02x}".format(*[int(max(0, min(255, c))) for c in rgb[:3]])


def _tsv(img: Path) -> list[dict]:
    r = subprocess.run(["tesseract", str(img), "stdout", "--psm", "3", "-l", "eng", "tsv"], capture_output=True, timeout=120)
    rows = []
    lines = r.stdout.decode(errors="replace").splitlines()
    if not lines:
        return rows
    head = lines[0].split("\t")
    for ln in lines[1:]:
        parts = ln.split("\t")
        if len(parts) != len(head):
            continue
        d = dict(zip(head, parts))
        txt = d.get("text", "").strip()
        try:
            conf = float(d.get("conf", -1))
        except ValueError:
            conf = -1
        if d.get("level") == "5" and txt and conf >= 40:
            rows.append({"block": int(d["block_num"]), "par": int(d["par_num"]), "line": int(d["line_num"]),
                         "x": int(d["left"]), "y": int(d["top"]), "w": int(d["width"]), "h": int(d["height"]), "text": txt})
    return rows


def slide_region(im: Image.Image) -> tuple[Image.Image, tuple[int, int, int, int]]:
    """Meeting recordings show the slide inside a frame (letterbox, webcam gallery, app chrome).
    Keep the largest area of the dominant background colour: that is the slide."""
    W, H = im.size
    gw, gh = 64, 36
    small = np.asarray(im.resize((gw * 4, gh * 4))).astype(np.int16)
    q = (small.reshape(-1, 3) // 24) * 24 + 12
    bg = np.array(Counter(map(tuple, q)).most_common(1)[0][0])
    near = (np.abs(small - bg).sum(2) < 60).reshape(gh, 4, gw, 4).mean((1, 3)) > 0.5
    seen = np.zeros_like(near)
    best: list = []
    for i in range(gh):
        for j in range(gw):
            if near[i, j] and not seen[i, j]:
                stack, comp = [(i, j)], []
                seen[i, j] = True
                while stack:
                    ci, cj = stack.pop()
                    comp.append((ci, cj))
                    for ni, nj in ((ci + 1, cj), (ci - 1, cj), (ci, cj + 1), (ci, cj - 1)):
                        if 0 <= ni < gh and 0 <= nj < gw and near[ni, nj] and not seen[ni, nj]:
                            seen[ni, nj] = True
                            stack.append((ni, nj))
                if len(comp) > len(best):
                    best = comp
    if len(best) < 0.25 * gw * gh:
        return im, (0, 0, W, H)
    i0, i1 = min(c[0] for c in best), max(c[0] for c in best) + 1
    j0, j1 = min(c[1] for c in best), max(c[1] for c in best) + 1
    box = (j0 * W // gw, i0 * H // gh, j1 * W // gw, i1 * H // gh)
    return im.crop(box), box


def describe(img_path: Path) -> dict:
    full = Image.open(img_path).convert("RGB")
    im, crop = slide_region(full)
    if crop != (0, 0) + full.size:
        img_path = img_path.with_name(img_path.stem + "-slide.png")
        im.save(img_path)
    W, H = im.size
    a = np.asarray(im).astype(np.int16)
    q = (a.reshape(-1, 3)[:: 7] // 16) * 16 + 8
    bg = np.array(Counter(map(tuple, q)).most_common(1)[0][0])
    words = _tsv(img_path)
    # OCR often reads side-by-side columns as one line: split a line where the words jump
    # across a wide gap, and keep each column as its own text block
    lines: dict[tuple, list] = defaultdict(list)
    raw: dict[tuple, list] = defaultdict(list)
    for w in words:
        raw[(w["block"], w["par"], w["line"])].append(w)
    for (b, p_, l_), ws in raw.items():
        ws = sorted(ws, key=lambda w: w["x"])
        col, prev = 0, None
        for w in ws:
            if prev is not None and w["x"] - (prev["x"] + prev["w"]) > 0.035 * W:
                col += 1
            lines[(b * 100 + col, p_, l_)].append(w)
            prev = w
    blocks: dict[int, list] = defaultdict(list)
    for (b, _p, _l), ws in sorted(lines.items(), key=lambda kv: (min(x["y"] for x in kv[1]), min(x["x"] for x in kv[1]))):
        x0, y0 = min(w["x"] for w in ws), min(w["y"] for w in ws)
        x1, y1 = max(w["x"] + w["w"] for w in ws), max(w["y"] + w["h"] for w in ws)
        blocks[b].append({"box": (x0, y0, x1, y1), "text": " ".join(w["text"] for w in sorted(ws, key=lambda w: w["x"])),
                          "h": float(np.median([w["h"] for w in ws]))})
    text_mask = np.zeros((H, W), bool)
    out_blocks = []
    for b, ls in blocks.items():
        x0, y0 = min(l["box"][0] for l in ls), min(l["box"][1] for l in ls)
        x1, y1 = max(l["box"][2] for l in ls), max(l["box"][3] for l in ls)
        text_mask[max(0, y0 - 4):y1 + 4, max(0, x0 - 4):x1 + 4] = True
        region = a[y0:y1, x0:x1].reshape(-1, 3)
        dist = np.abs(region - bg).sum(1) if len(region) else np.zeros(0)
        ink = region[dist > np.percentile(dist, 85)] if len(region) > 10 else region
        color = ink.mean(0) if len(ink) else np.array([0, 0, 0])
        size = float(np.median([l["h"] for l in ls])) / H
        centre = abs(((x0 + x1) / 2) / W - 0.5) < 0.06 and (x1 - x0) / W < 0.8
        out_blocks.append({"box": [round(x0 / W, 3), round(y0 / H, 3), round((x1 - x0) / W, 3), round((y1 - y0) / H, 3)],
                           "lines": [l["text"] for l in ls], "size": round(size, 4), "color": _hex(color),
                           "align": "center" if centre else "left"})
    # picture regions: busy cells that are not text
    gh, gw = 18, 32
    diff = np.abs(a - bg).sum(2) > 60
    busy = np.zeros((gh, gw), bool)
    var = np.zeros((gh, gw))
    for i in range(gh):
        for j in range(gw):
            ys, ye, xs, xe = i * H // gh, (i + 1) * H // gh, j * W // gw, (j + 1) * W // gw
            cell = diff[ys:ye, xs:xe] & ~text_mask[ys:ye, xs:xe]
            busy[i, j] = cell.mean() > 0.25
            var[i, j] = a[ys:ye, xs:xe].std()
    seen = np.zeros_like(busy)
    images = []
    for i in range(gh):
        for j in range(gw):
            if not busy[i, j] or seen[i, j]:
                continue
            stack, cells = [(i, j)], []
            seen[i, j] = True
            while stack:
                ci, cj = stack.pop()
                cells.append((ci, cj))
                for ni, nj in ((ci + 1, cj), (ci - 1, cj), (ci, cj + 1), (ci, cj - 1)):
                    if 0 <= ni < gh and 0 <= nj < gw and busy[ni, nj] and not seen[ni, nj]:
                        seen[ni, nj] = True
                        stack.append((ni, nj))
            if len(cells) < 3:
                continue
            i0, i1 = min(c[0] for c in cells), max(c[0] for c in cells) + 1
            j0, j1 = min(c[1] for c in cells), max(c[1] for c in cells) + 1
            ys, ye, xs, xe = i0 * H // gh, i1 * H // gh, j0 * W // gw, j1 * W // gw
            px = a[ys:ye, xs:xe].reshape(-1, 3)
            mean = px.mean(0)
            colours = len({tuple(c) for c in (px[:: 5] // 32)})
            images.append({"box": [round(j0 / gw, 3), round(i0 / gh, 3), round((j1 - j0) / gw, 3), round((i1 - i0) / gh, 3)],
                           "kind": "photo" if colours > 60 and np.mean([var[c] for c in cells]) > 45 else "graphic",
                           "color": _hex(mean)})
    title = None
    top = [b for b in out_blocks if b["box"][1] < 0.3]
    if top:
        tb = max(top, key=lambda b: b["size"])
        if tb["size"] >= 1.2 * float(np.median([b["size"] for b in out_blocks])):
            title = " ".join(tb["lines"])
            tb["role"] = "title"
    g = np.gcd(W, H)
    spec = {"canvas": {"aspect": f"{W // g}:{H // g}" if W // g < 50 else f"{W / H:.2f}:1", "background": _hex(bg),
                       "cropped_from_frame": list(crop) if crop != (0, 0) + full.size else None},
            "title": title, "text": out_blocks, "pictures": images[:12]}
    return spec


def prompt(spec: dict) -> str:
    c = spec["canvas"]
    parts = [f"A {c['aspect']} presentation slide with a {c['background']} background."]
    if spec.get("title"):
        parts.append(f'Title: "{spec["title"]}".')
    def where(box):
        x, y, w, h = box
        col = "left" if x + w / 2 < 0.38 else "right" if x + w / 2 > 0.62 else "centre"
        row = "top" if y + h / 2 < 0.33 else "bottom" if y + h / 2 > 0.66 else "middle"
        return f"{row} {col}"
    for b in spec["text"]:
        if b.get("role") == "title":
            continue
        txt = " / ".join(b["lines"])
        parts.append(f'Text at {where(b["box"])} ({b["color"]}, {"large" if b["size"] > 0.05 else "small"}): "{txt}".')
    for p in spec["pictures"]:
        parts.append(f'A {p["kind"]} at {where(p["box"])}, about {int(p["box"][2] * 100)}% wide, mostly {p["color"]}.')
    return " ".join(parts)


def render(spec: dict, out: Path, width: int = 1600) -> Path:
    num, den = spec["canvas"]["aspect"].split(":")
    ratio = float(num) / float(den)
    W, H = width, int(round(width / ratio))
    im = Image.new("RGB", (W, H), spec["canvas"]["background"])
    d = ImageDraw.Draw(im)
    for p in spec["pictures"]:
        x, y, w, h = p["box"]
        box = (int(x * W), int(y * H), int((x + w) * W), int((y + h) * H))
        d.rounded_rectangle(box, radius=8, fill=p["color"], outline="#9aa5b1", width=2)
        d.text((box[0] + 8, box[1] + 6), p["kind"], fill="#ffffff" if sum(int(p["color"][i:i + 2], 16) for i in (1, 3, 5)) < 380 else "#333333",
               font=_font(14))
    for b in spec["text"]:
        x, y, w, h = b["box"]
        size = max(13, int(b["size"] * H * 0.95))
        f = _font(size, bold=b.get("role") == "title")
        n = max(1, len(b["lines"]))
        step = max(size + 2, int(h * H / n))
        for k, line in enumerate(b["lines"]):
            ty = int(y * H) + k * step
            tw = d.textlength(line, font=f)
            tx = int((x + w / 2) * W - tw / 2) if b.get("align") == "center" else int(x * W)
            d.text((tx, ty), line, fill=b["color"], font=f)
    im.save(out)
    return out


def _tok(s: str) -> set[str]:
    import re
    return {t for t in re.findall(r"[a-z0-9]{3,}", (s or "").lower())}


def run(img_path: Path, out: Path) -> dict:
    spec = describe(img_path)
    text = prompt(spec)
    rec = render(spec, out / "recreated.png")
    o = subprocess.run(["tesseract", str(rec), "stdout", "--psm", "3", "-l", "eng"], capture_output=True, timeout=120)
    want = _tok(" ".join(l for b in spec["text"] for l in b["lines"]))
    got = _tok(o.stdout.decode(errors="replace"))
    return {"spec": spec, "prompt": text, "image": rec.name,
            "measured": {"spec_bytes": len(json.dumps(spec, separators=(",", ":")).encode()),
                         "prompt_bytes": len(text.encode()), "frame_bytes": img_path.stat().st_size,
                         "text_recall": round(len(want & got) / len(want), 3) if want else None,
                         "words": len(want)}}
