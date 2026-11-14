"""Text normalisation, tokenisation and shingling shared by dedup, diff and search."""
from __future__ import annotations

import hashlib
import re
import unicodedata

_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9][a-z0-9\-_/.]*[a-z0-9]|[a-z0-9]")
STOP = frozenset("""a an and are as at be but by for from has have i in is it its of on or that the this to was
were will with we you our your they them their there here so if not no do does did can could should would
then than also into about over just more most some such only own same very s t""".split())


def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = t.replace("­", "")          # soft hyphen
    return _WS.sub(" ", t).strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()


def tokens(text: str, drop_stop: bool = True) -> list[str]:
    toks = _TOKEN.findall(normalize(text))
    out = []
    for t in toks:
        if drop_stop and t in STOP:
            continue
        if len(t) > 4 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]                    # crude plural folding
        out.append(t[:64])
    return out


def shingles(text: str, k: int = 3) -> set[str]:
    toks = tokens(text, drop_stop=False)
    if len(toks) < k:
        return {" ".join(toks)} if toks else set()
    return {" ".join(toks[i:i + k]) for i in range(len(toks) - k + 1)}
