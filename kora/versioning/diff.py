"""Slide-level diff between two versions of a document.

1. Align on body_hash (footer-insensitive) with a longest-matching-block
   sequence alignment -> "unchanged" / "unchanged_body" (only footer or page
   number differs, e.g. because a slide was removed earlier in the deck).
2. Identical bodies that appear in both but out of order -> "moved".
3. Remaining slides are paired greedily by similarity (token Jaccard on text,
   perceptual-hash distance for image-only slides) -> "modified" with a line diff.
4. Leftovers -> "removed" (only in old) / "added" (only in new).
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass

from ..fingerprint.phash import hamming
from ..fingerprint.text import tokens

MODIFIED_MIN_SIMILARITY = 0.45


@dataclass
class UnitView:
    ordinal: int
    unit_hash: str | None
    body_hash: str | None
    text: str
    phash: str | None = None

    @property
    def title(self) -> str:
        for line in (self.text or "").splitlines():
            if line.strip():
                return line.strip()[:120]
        return f"(slide {self.ordinal})"


def similarity(a: UnitView, b: UnitView) -> float:
    ta, tb = set(tokens(a.text)), set(tokens(b.text))
    text_sim = len(ta & tb) / len(ta | tb) if (ta or tb) else 0.0
    vis_sim = 0.0
    if a.phash and b.phash:
        d = hamming(a.phash, b.phash)
        vis_sim = max(0.0, 1 - d / 24)
    if not ta and not tb:
        return vis_sim
    return max(text_sim, 0.85 * text_sim + 0.15 * vis_sim)


def _line_diff(a: str, b: str, limit: int = 40) -> list[str]:
    out = []
    for line in difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        out.append(line)
        if len(out) >= limit:
            break
    return out


def diff(old: list[UnitView], new: list[UnitView]) -> dict:
    sm = difflib.SequenceMatcher(a=[u.body_hash for u in old], b=[u.body_hash for u in new], autojunk=False)
    unchanged, unchanged_body = [], []
    ua, ub = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for i, j in zip(range(i1, i2), range(j1, j2)):
                rec = {"from": old[i].ordinal, "to": new[j].ordinal, "title": new[j].title}
                (unchanged if old[i].unit_hash == new[j].unit_hash else unchanged_body).append(rec)
        else:
            ua.extend(range(i1, i2))
            ub.extend(range(j1, j2))
    moved = []
    by_body_b: dict[str, list[int]] = {}
    for j in ub:
        by_body_b.setdefault(new[j].body_hash, []).append(j)
    for i in list(ua):
        js = by_body_b.get(old[i].body_hash)
        if js:
            j = js.pop(0)
            moved.append({"from": old[i].ordinal, "to": new[j].ordinal, "title": new[j].title})
            ua.remove(i)
            ub.remove(j)
    cands = sorted(((similarity(old[i], new[j]), i, j) for i in ua for j in ub), reverse=True)
    used_a, used_b, modified = set(), set(), []
    for sim, i, j in cands:
        if sim < MODIFIED_MIN_SIMILARITY:
            break
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        modified.append({"from": old[i].ordinal, "to": new[j].ordinal, "title": new[j].title,
                         "similarity": round(sim, 3), "text_diff": _line_diff(old[i].text, new[j].text)})
    removed = [{"from": old[i].ordinal, "title": old[i].title} for i in ua if i not in used_a]
    added = [{"to": new[j].ordinal, "title": new[j].title} for j in ub if j not in used_b]
    same = len(unchanged) + len(unchanged_body) + len(moved)
    denom = max(1, len(old) + len(new) - same)
    return {
        "from_slides": len(old), "to_slides": len(new),
        "unchanged": len(unchanged), "unchanged_body": len(unchanged_body),
        "moved": moved, "modified": sorted(modified, key=lambda r: r["to"]),
        "added": added, "removed": removed,
        "similarity": round(same / denom, 3),
        "summary": (f"{len(unchanged) + len(unchanged_body)} unchanged"
                    + (f" ({len(unchanged_body)} footer-only)" if unchanged_body else "")
                    + f", {len(moved)} moved, {len(modified)} modified, {len(added)} added, {len(removed)} removed"),
    }
