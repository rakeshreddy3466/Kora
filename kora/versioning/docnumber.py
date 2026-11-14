"""Document-control metadata detection (Ericsson style).

Document numbers look like
  1/1551-LZA 701 0001 Uen      (sequence / decimal class - product number, language)
  EN/LZT 123 4567 R1A
  2/0363-FGB 101 1234
Revisions: "Rev A", "Revision PA2", "rev:B", "R1A".
Sources in order of trust: repository metadata > document properties > first/last page text.
"""
from __future__ import annotations

import re

_DOCNO = re.compile(
    r"\b((?:\d{1,4}/)?\d{3}\s?\d{0,2}-[A-Z]{2,4}\s?\d{3}\s?\d{2,4}(?:/\d{1,3})?(?:\s?Uen|\s?[A-Z][a-z]{2})?"
    r"|[A-Z]{2,3}/[A-Z]{3}\s?\d{3}\s?\d{3,4}(?:/\d{1,3})?"
    r"|[A-Z]{2,5}-\d{2}:\d{3,7}(?:\s?Uen)?)\b")          # e.g. EAB-24:000123 Uen
_REV = re.compile(r"\b(?:Rev(?:ision)?\.?\s*[:\-]?\s*|rev:)(P?[A-HJ-NP-Z]{1,2}\d{0,3}|[RP]\d{1,2}[A-Z]{1,2}\d{0,2})\b")


def normalize_docno(d: str) -> str:
    return re.sub(r"\s+", " ", d.strip()).upper()


def find_docno(text: str) -> str | None:
    m = _DOCNO.search(text or "")
    return normalize_docno(m.group(1)) if m else None


_FOOTER_REV = re.compile(r"\s*[|,;]\s*(P?[A-HJ-NP-Z]{1,2}\d{0,3}|[RP]\d{1,2}[A-Z]{1,2}\d{0,2})\s*(?:[|,;]|$)")


def find_revision(text: str) -> str | None:
    m = _REV.search(text or "")
    if m:
        return m.group(1).upper()
    # Ericsson template footer: "<doc number> | <revision> | <date> | ..."
    d = _DOCNO.search(text or "")
    if d:
        f = _FOOTER_REV.match(text, d.end())
        if f:
            return f.group(1).upper()
    return None


def detect(meta: dict, first_text: str, last_text: str, repo_meta: dict | None = None) -> dict:
    """Return {"doc_number", "revision", "source"} with the most trusted evidence."""
    repo_meta = repo_meta or {}
    out: dict = {}
    if repo_meta.get("doc_number"):
        out["doc_number"], out["doc_number_source"] = normalize_docno(repo_meta["doc_number"]), "repository"
    if repo_meta.get("revision"):
        out["revision"], out["revision_source"] = repo_meta["revision"].upper(), "repository"
    if "doc_number" not in out and meta.get("doc_number_prop"):
        out["doc_number"], out["doc_number_source"] = normalize_docno(meta["doc_number_prop"]), "doc_metadata"
    if "revision" not in out and meta.get("revision_prop"):
        out["revision"], out["revision_source"] = str(meta["revision_prop"]).upper(), "doc_metadata"
    props = " ".join(str(meta.get(k) or "") for k in ("subject", "keywords", "identifier", "title", "version"))
    if "doc_number" not in out:
        d = find_docno(props)
        if d:
            out["doc_number"], out["doc_number_source"] = d, "doc_metadata"
    if "revision" not in out:
        r = find_revision(props)
        if r:
            out["revision"], out["revision_source"] = r, "doc_metadata"
    if "doc_number" not in out:
        d = find_docno(first_text) or find_docno(last_text)
        if d:
            out["doc_number"], out["doc_number_source"] = d, "doc_text"
    if "revision" not in out:
        r = find_revision(first_text) or find_revision(last_text)
        if r:
            out["revision"], out["revision_source"] = r, "doc_text"
    return out
