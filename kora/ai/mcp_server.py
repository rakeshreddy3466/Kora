"""KORA as an MCP server (stdio, JSON-RPC 2.0), so AI assistants such as Claude can use
the organisation's knowledge with the same rules as a person.

    KORA_API_KEY=kora_... KORA_DATA_DIR=... kora mcp

The key decides who is asking: results are filtered by that principal's permissions,
limited to AI-opted-in sources, PII-redacted and cited (see ai/context.py). The server
is read-only: it cannot plan, approve or delete anything. Dependency-free on purpose;
it implements the small part of the protocol a tools-only server needs.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

from sqlalchemy import select

from .. import __version__, bootstrap, db
from ..models import Capsule, Content, File, Lineage, Unit, Version
from ..queries import display_name
from ..security.acl import can_read
from ..security.auth import authenticate
from .context import ai_sources, context

PROTOCOL = "2025-06-18"

TOOLS = [
    {"name": "search_knowledge",
     "description": "Search the organisation's documents, slides, recordings and chats that this user may read. "
                    "Returns numbered, cited passages (document number, revision, page or timestamp). "
                    "Answer only from them and cite [n].",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "what to look for"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 8}},
         "required": ["query"]}},
    {"name": "document_versions",
     "description": "Which revision of a document is current, and how revisions differ. Use before quoting "
                    "a document, so the answer is based on the current revision.",
     "inputSchema": {"type": "object", "properties": {
         "doc_number": {"type": "string", "description": "document number or title fragment"}},
         "required": ["doc_number"]}},
    {"name": "get_capsule",
     "description": "The knowledge capsule of one content (from a search result's cite.content): its slides, "
                    "keyframes and transcript as text. Works even when the original file was deleted.",
     "inputSchema": {"type": "object", "properties": {
         "content": {"type": "string", "description": "64-hex content id"}},
         "required": ["content"]}},
]


class ToolError(Exception):
    pass


def _readable(s, ident, sha: str) -> bool:
    toks = ident.acl_tokens
    if any(can_read(toks, f.acl) for f in s.scalars(select(File).where(File.sha256 == sha))):
        return True
    cap = s.get(Capsule, sha)
    return bool(cap and can_read(toks, cap.manifest.get("acl_snapshot", [])))


def _ident(s):
    ident = authenticate(s, os.environ.get("KORA_API_KEY"))
    if ident is None:
        raise ToolError("KORA_API_KEY is missing, revoked or invalid")
    return ident


def call(name: str, args: dict[str, Any]) -> Any:
    with db.session_scope() as s:
        ident = _ident(s)
        if name == "search_knowledge":
            q = str(args.get("query", "")).strip()
            if len(q) < 2:
                raise ToolError("query is too short")
            r = context(s, ident, q[:300], int(args.get("limit", 8)))
            return {"instructions": r["instructions"], "passages": r["context_block"] or "(nothing found)",
                    "citations": [{"n": c["n"], **c["cite"]} for c in r["chunks"]],
                    "excluded": r["excluded"]}
        if name == "document_versions":
            needle = str(args.get("doc_number", "")).strip().lower()
            if len(needle) < 2:
                raise ToolError("doc_number is too short")
            out = []
            for lin in s.scalars(select(Lineage)):
                hay = f"{lin.doc_number or ''} {lin.title or ''} {lin.key}".lower()
                if needle not in hay:
                    continue
                vs = sorted(lin.versions, key=lambda v: v.ordinal)
                if not any(_readable(s, ident, v.content_sha) for v in vs):
                    continue
                out.append({"doc_number": lin.doc_number, "title": lin.title,
                            "ordering": f"{lin.ordering_basis} ({lin.ordering_confidence} confidence)",
                            "conflict": lin.has_conflict,
                            "versions": [{"revision": v.revision_label, "current": v.is_head, "released": v.released,
                                          "name": display_name(s, v.content_sha), "content": v.content_sha,
                                          "change": (v.diff_from_parent or {}).get("summary")} for v in vs]})
            return {"lineages": out[:10]} if out else {"lineages": [], "note": "no matching document family"}
        if name == "get_capsule":
            sha = str(args.get("content", ""))
            from ..hitl import known_names
            from ..privacy import pii
            if len(sha) != 64 or not _readable(s, ident, sha):
                raise ToolError("not found")
            files = {f.source_name for f in s.scalars(select(File).where(File.sha256 == sha))}
            if not files & ai_sources(s):
                raise ToolError("this content's source is not opted in for AI use")
            cap, c = s.get(Capsule, sha), s.get(Content, sha)
            if cap is None or c is None:
                raise ToolError("no capsule for this content")
            names = known_names(s)
            units = [{"kind": u.kind, "n": u.ordinal, "t": u.start_s, "text": pii.redact(u.text or "", names)}
                     for u in s.scalars(select(Unit).where(Unit.content_sha == sha).order_by(Unit.kind, Unit.ordinal))
                     if (u.text or "").strip()][:300]
            v = s.scalar(select(Version).where(Version.content_sha == sha))
            return {"name": display_name(s, sha), "type": c.detected_type, "doc_number": (c.meta or {}).get("doc_number"),
                    "revision": v.revision_label if v else None, "current": bool(v.is_head) if v else None,
                    "capsule_valid": cap.valid, "units": units}
        raise ToolError(f"unknown tool {name}")


def _reply(mid, result=None, error=None) -> None:
    msg = {"jsonrpc": "2.0", "id": mid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sys.stdout.write(json.dumps(msg, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


def handle(msg: dict) -> None:
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:                                   # notification (e.g. notifications/initialized)
        return
    if method == "initialize":
        want = (msg.get("params") or {}).get("protocolVersion") or PROTOCOL
        _reply(mid, {"protocolVersion": want, "capabilities": {"tools": {"listChanged": False}},
                     "serverInfo": {"name": "kora", "version": __version__},
                     "instructions": "Organisational knowledge with permissions, provenance and versions. "
                                     "Cite passages as [n]; prefer current revisions."})
    elif method == "ping":
        _reply(mid, {})
    elif method == "tools/list":
        _reply(mid, {"tools": TOOLS})
    elif method == "tools/call":
        p = msg.get("params") or {}
        try:
            res = call(p.get("name", ""), p.get("arguments") or {})
            _reply(mid, {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False, default=str)}],
                         "structuredContent": res if isinstance(res, dict) else {"result": res}, "isError": False})
        except ToolError as e:
            _reply(mid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
        except Exception as e:                        # never crash the session; no stack traces to the client
            print(f"kora mcp: tool error: {type(e).__name__}: {e}", file=sys.stderr)
            _reply(mid, {"content": [{"type": "text", "text": "internal error"}], "isError": True})
    else:
        _reply(mid, error={"code": -32601, "message": f"method not found: {method}"})


def serve() -> None:
    bootstrap.init(json_logs=True)
    import logging
    for h in logging.getLogger().handlers:            # stdout belongs to the protocol
        if getattr(h, "stream", None) is sys.stdout:
            h.setStream(sys.stderr)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _reply(None, error={"code": -32700, "message": "parse error"})
            continue
        if isinstance(msg, list):
            for m in msg:
                handle(m)
        else:
            handle(msg)
