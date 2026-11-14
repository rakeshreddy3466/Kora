"""Process bootstrap shared by API, worker and CLI."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from . import audit, clock, db
from .config import Settings, configure, get_settings
from .jobs import enqueue
from .logs import setup_logging
from .models import Principal, Source

_ready = False


def init(settings: Settings | None = None, json_logs: bool = True) -> Settings:
    global _ready
    if settings is not None:
        configure(settings)
    st = get_settings()
    setup_logging(st.log_level, json_logs)
    db.init_engine(st.database_url)
    db.create_all()
    from . import pipeline  # noqa: F401  registers job handlers
    from .storage import reset_blobstore
    reset_blobstore()
    _upgrade_data()
    _ready = True
    return st


def _upgrade_data() -> None:
    """One-off upgrades of data extracted by an older KORA (each runs once, then is a no-op)."""
    from .knowledge import notes
    try:
        with db.session_scope() as s:
            n = notes.backfill(s)
            from sqlalchemy import select
            from . import hitl
            from .models import ReviewTask
            for t in s.scalars(select(ReviewTask).where(ReviewTask.kind == "pii_review", ReviewTask.status == "open")):
                if "_METADATA:" in t.question or "EMAIL:" in t.question or "PHONE:" in t.question:
                    hitl.generate(s, t.subject_id)          # older wording listed raw codes in the question
        if n:
            logging.getLogger("kora").info("speaker notes added for %d deck(s)", n)
    except Exception:                                       # never block start-up on an upgrade
        logging.getLogger("kora").exception("speaker-notes upgrade failed; it will be retried next start")


def upsert_principal(s: Session, d: dict) -> Principal:
    p = s.get(Principal, d["id"])
    if p is None:
        p = Principal(id=d["id"], display_name=d.get("display_name", d["id"]))
        s.add(p)
    for k in ("display_name", "email", "status", "groups", "manager_id", "team"):
        if k in d:
            setattr(p, k, d[k])
    return p


def upsert_source(s: Session, name: str, system: str, root: str, site: str | None = None,
                  approved: bool = True, allow_actions: bool = True) -> Source:
    root_p = Path(root).resolve()
    if not root_p.is_dir():
        raise ValueError(f"source root {root_p} is not a directory")
    src = s.get(Source, name)
    cfg = {"root": str(root_p), "site": site, "allow_actions": allow_actions}
    if src is not None and (src.config or {}).get("ai_use_allowed"):
        cfg["ai_use_allowed"] = True                     # a governance decision survives re-registration
    if src is None:
        src = Source(name=name, system=system, connector="filesystem", config=cfg, approved_repository=approved)
        s.add(src)
        audit.append(s, "admin", "source.added", "source", name, {"system": system, "root": str(root_p)})
    else:
        src.system, src.config, src.approved_repository = system, cfg, approved
    return src


def load_samples(s: Session, root: Path) -> None:
    root = Path(root)
    for d in json.loads((root / "principals.json").read_text()):
        upsert_principal(s, d)
    for d in json.loads((root / "sources.json").read_text()):
        upsert_source(s, d["name"], d["system"], str(root / d["root"]), d.get("site"))


def scan_all(s: Session) -> list[str]:
    ids = []
    for src in s.query(Source).filter(Source.enabled.is_(True)):
        ids.append(enqueue(s, "source.scan", {"source": src.name}, key=f"scan:{src.name}:{clock.now().isoformat()}",
                           priority=10))
    return ids
