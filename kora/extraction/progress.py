"""Progress from inside the sandbox: a tiny JSON file the parent's keep-alive thread reads.
Best effort only: a failure to write progress never fails the extraction."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

FILE = "progress.json"


def report(out: Path | None, step: str, done: int | None = None, total: int | None = None,
           unit: str | None = None) -> None:
    """unit "s" means done/total are seconds of media (shown as m:ss); otherwise they are a count."""
    if out is None:
        return
    try:
        tmp = out / (FILE + ".tmp")
        tmp.write_text(json.dumps({"step": step[:120], "done": done, "total": total, "unit": unit,
                                   "at": time.time()}))
        os.replace(tmp, out / FILE)
    except OSError:
        pass


def read(out: Path) -> dict | None:
    try:
        return json.loads((out / FILE).read_text())
    except (OSError, ValueError):
        return None
