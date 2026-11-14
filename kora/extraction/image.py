from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image

from ..fingerprint.phash import phash
from .entry import PermanentInput

Image.MAX_IMAGE_PIXELS = 120_000_000   # decompression-bomb guard


def extract(path: Path, out: Path, opts: dict) -> dict:
    try:
        with Image.open(path) as im:
            im.load()
            ph = phash(im)
            w, h = im.size
            thumb = out / "thumb-0001.jpg"
            t = im.convert("RGB")
            t.thumbnail((640, 640))
            t.save(thumb, "JPEG", quality=70)
            rh = hashlib.sha256(im.convert("L").tobytes()).hexdigest()
    except Image.DecompressionBombError as e:
        raise PermanentInput(str(e)) from e
    except Exception as e:
        raise PermanentInput(f"unreadable image: {e}") from e
    uh = hashlib.sha256(f"image|{rh}".encode()).hexdigest()
    return {"type": "image", "metadata": {"width": w, "height": h},
            "units": [{"kind": "image", "ordinal": 1, "text": "", "text_hash": None, "unit_hash": uh,
                       "phash": ph, "thumb": thumb.name, "blob": None, "meta": {"width": w, "height": h}}],
            "normalized_hash": hashlib.sha256(uh.encode()).hexdigest()}
