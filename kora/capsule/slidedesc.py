"""Describe a recording's slide frame as text and recreate it from that text (parent side).

The heavy work (OCR, layout, rendering) runs in the sandbox; the result is cached on the unit,
and the recreated image is a hot-tier blob readable by whoever can read the recording."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from .. import db
from ..models import Unit
from ..security import sandbox
from ..storage import get_blobstore


class NotAFrame(ValueError):
    pass


def describe(unit_id: str, refresh: bool = False) -> dict:
    with db.session_scope() as s:
        u = s.get(Unit, unit_id)
        if u is None or u.kind != "frame" or not u.blob_key:
            raise NotAFrame("slide descriptions are made from recording frames")
        if (u.meta or {}).get("spec") and not refresh:
            return u.meta["spec"]
        key = u.blob_key
    store = get_blobstore()
    work = Path(tempfile.mkdtemp(prefix="kora-spec-"))
    try:
        src = work / "frame.jpg"
        store.write_to("hot", key, src)
        out = work / "out"
        out.mkdir()
        res = sandbox.run("slide_spec", {"path": str(src)}, out)
        img = (out / res["image"]).resolve()
        if not img.is_relative_to(out.resolve()):
            raise ValueError("invalid sandbox output")
        ref = store.put_file("hot", img)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    spec = {"spec": res["spec"], "prompt": res["prompt"], "image": ref.key, "image_bytes": ref.size,
            "measured": res["measured"]}
    with db.session_scope() as s:
        u = s.get(Unit, unit_id)
        u.meta = {**(u.meta or {}), "spec": spec}
    return spec
