"""Word documents and Outlook messages (e.g. a meeting's chat history mailed out).

docx: paragraphs and tables, chunked into page-like units.
msg : headers + body; Teams-style chat lines "[HH:MM] Name" become one
      `message` unit each, with author (as speaker) and time, so chat is
      searchable, attributable and covered by access/erasure requests.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from ..fingerprint.text import text_hash
from .entry import PermanentInput

_CHAT = re.compile(r"^\s*\[(\d{1,2}:\d{2}(?::\d{2})?)\]\s+(.{2,80}?)\s*$")


def _unit(kind: str, n: int, text: str, **extra) -> dict:
    return {"kind": kind, "ordinal": n, "text": text, "text_hash": text_hash(text),
            "unit_hash": hashlib.sha256(f"{kind}|".encode() + text.encode()).hexdigest(), "body_hash": None,
            "phash": None, "thumb": None, "blob": None, "meta": extra.pop("meta", {}), **extra}


def _chunks(text: str, size: int = 3000) -> list[str]:
    paras, out, cur = text.split("\n"), [], ""
    for p in paras:
        if len(cur) + len(p) > size and cur:
            out.append(cur)
            cur = ""
        cur += p + "\n"
    if cur.strip():
        out.append(cur)
    return out or [""]


def custom_props(path: Path) -> dict:
    """docProps/custom.xml: Ericsson templates keep DocNo, Revision, Prepared, Date and the
    confidentiality label here - more trustworthy than file names or slide text."""
    import zipfile
    from xml.etree import ElementTree as ET
    try:
        with zipfile.ZipFile(path) as z:
            if "docProps/custom.xml" not in z.namelist():
                return {}
            root = ET.fromstring(z.read("docProps/custom.xml")[:2_000_000])
    except Exception:
        return {}
    out = {}
    for prop in root:
        name = prop.get("name")
        val = next((child.text for child in prop if child.text), None)
        if name and val:
            out[name] = val.strip()[:500]
    return out


def ericsson_meta(props: dict) -> dict:
    m = {}
    if props.get("DocNo"):
        m["doc_number_prop"] = props["DocNo"]
    if props.get("Revision"):
        m["revision_prop"] = props["Revision"]
    elif props.get("LeftFooterField") and "Rev" in props["LeftFooterField"]:
        m["revision_prop"] = props["LeftFooterField"].rsplit("Rev", 1)[1].strip(" .,:")
    for k_in, k_out in (("Prepared", "prepared_by"), ("Date", "doc_date"), ("DocType", "doc_type")):
        if props.get(k_in):
            m[k_out] = props[k_in]
    label = props.get("ExtConf") or props.get("txtConfLabel")
    if label:
        m["classification"] = label
    return m


def _doc(kind: str, units: list[dict], meta: dict) -> dict:
    return {"type": kind, "metadata": meta, "units": units,
            "normalized_hash": hashlib.sha256("|".join(u["unit_hash"] for u in units).encode()).hexdigest()}


def docx(path: Path) -> dict:
    import docx as _docx
    try:
        d = _docx.Document(str(path))
    except Exception as e:
        raise PermanentInput(f"unreadable docx: {e}") from e
    parts = [p.text for p in d.paragraphs if p.text.strip()]
    for t in d.tables:
        for row in t.rows:
            parts.append(" | ".join(c.text.strip() for c in row.cells))
    cp = d.core_properties
    meta = {k: v for k, v in {"title": cp.title, "author": cp.author, "subject": cp.subject, "keywords": cp.keywords,
                              "identifier": cp.identifier, "version": cp.version, "last_modified_by": cp.last_modified_by,
                              "created": cp.created.isoformat() if cp.created else None,
                              "modified": cp.modified.isoformat() if cp.modified else None}.items() if v}
    meta.update(ericsson_meta(custom_props(path)))
    units = [_unit("page", i, t) for i, t in enumerate(_chunks("\n".join(parts)), 1)]
    return _doc("docx", units, meta)


def msg(path: Path) -> dict:
    import extract_msg
    try:
        m = extract_msg.Message(str(path))
    except Exception as e:
        raise PermanentInput(f"unreadable msg: {e}") from e
    body = (m.body or "").replace("\r\n", "\n")
    meta = {k: str(v) for k, v in {"title": m.subject, "sender": m.sender, "to": m.to, "date": m.date}.items() if v}
    meta["attachments"] = [a.longFilename or a.shortFilename for a in m.attachments]
    units, n, cur, intro = [], 0, None, []
    for line in body.split("\n"):
        hit = _CHAT.match(line)
        if hit:
            if cur:
                n += 1
                units.append(_unit("message", n, f"{cur['author']}: {cur['text'].strip()}", speaker=cur["author"],
                                   meta={"time": cur["time"]}))
            cur = {"time": hit.group(1), "author": hit.group(2).strip(), "text": ""}
        elif cur is not None:
            cur["text"] += line.strip() + " " if line.strip() else ""
        else:
            intro.append(line)
    if cur:
        n += 1
        units.append(_unit("message", n, f"{cur['author']}: {cur['text'].strip()}", speaker=cur["author"],
                           meta={"time": cur["time"]}))
    head = "\n".join([meta.get("title", "")] + [l for l in intro if l.strip()])
    units.insert(0, _unit("page", 0, head))
    if not n:                                    # not a chat: keep the whole body
        units = [_unit("page", i, t) for i, t in enumerate(_chunks(meta.get("title", "") + "\n" + body), 1)]
    meta["chat_messages"] = n
    return _doc("msg", units, meta)
