"""Relational model.

Key separation:
  File     - one *occurrence* of bytes at a location in a source system
             (SharePoint path, OneDrive path, Ericsson Play item). Mutable state.
  Content  - the bytes themselves, addressed by SHA-256. Extraction happens
             once per Content, so N exact copies cost one extraction.
  Unit     - a page / slide / keyframe / transcript segment of a Content.
             Slide-level identity lives here (unit_hash).
  SlideBlob- the slide store: one render-equivalent single-page PDF per unique
             unit_hash. Revisions and decks that share slides share blobs.
  Lineage/Version - document families and their ordered revisions.
  Relation - typed edges of the content relationship graph.
  DeletionPlan/Item - governed, evidence-carrying deletion.
  Job      - durable work queue (transactional with domain writes).
  AuditEvent - append-only, hash-chained, MAC'd audit log.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer,
                        LargeBinary, String, Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from . import clock


class Base(DeclarativeBase):
    pass


def new_id() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------- identity
class Principal(Base):
    __tablename__ = "principals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|departing|departed
    groups: Mapped[list] = mapped_column(JSON, default=list)
    manager_id: Mapped[str | None] = mapped_column(String(64))
    team: Mapped[str | None] = mapped_column(String(100))


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    principal_id: Mapped[str] = mapped_column(ForeignKey("principals.id"))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    roles: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime)


# --------------------------------------------------------------------------- sources & files
class Source(Base):
    __tablename__ = "sources"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    system: Mapped[str] = mapped_column(String(32))          # sharepoint|onedrive|ericsson_play|upload
    connector: Mapped[str] = mapped_column(String(32), default="filesystem")
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    approved_repository: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_scan_at: Mapped[datetime | None] = mapped_column(DateTime)


class File(Base):
    __tablename__ = "files"
    __table_args__ = (UniqueConstraint("source_name", "path", name="uq_file_location"),
                      Index("ix_files_sha_status", "sha256", "status"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source_name: Mapped[str] = mapped_column(ForeignKey("sources.name"))
    system: Mapped[str] = mapped_column(String(32))
    site: Mapped[str | None] = mapped_column(String(200))
    path: Mapped[str] = mapped_column(String(1024))
    filename: Mapped[str] = mapped_column(String(512))
    extension: Mapped[str] = mapped_column(String(16), default="")
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str | None] = mapped_column(ForeignKey("contents.sha256"))
    detected_type: Mapped[str | None] = mapped_column(String(32))
    owner_id: Mapped[str | None] = mapped_column(String(64))
    acl: Mapped[list] = mapped_column(JSON, default=list)      # ["user:alice","group:network-team","everyone"]
    retention_label: Mapped[str] = mapped_column(String(64), default="standard")
    legal_hold: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at_source: Mapped[datetime | None] = mapped_column(DateTime)
    modified_at_source: Mapped[datetime | None] = mapped_column(DateTime)
    source_etag: Mapped[str | None] = mapped_column(String(128))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    # active | quarantined | purged | missing
    status: Mapped[str] = mapped_column(String(16), default="active")
    # registered | hashed | extracting | ready | unsupported | rejected | failed
    ingest_state: Mapped[str] = mapped_column(String(16), default="registered")
    last_error: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    row_version: Mapped[int] = mapped_column(Integer, default=1)

    content: Mapped["Content | None"] = relationship(back_populates="files")
    __mapper_args__ = {"version_id_col": row_version}


# --------------------------------------------------------------------------- content & units
class Content(Base):
    __tablename__ = "contents"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    detected_type: Mapped[str | None] = mapped_column(String(32))
    extracted: Mapped[bool] = mapped_column(Boolean, default=False)
    extractor_version: Mapped[int] = mapped_column(Integer, default=0)
    unit_count: Mapped[int] = mapped_column(Integer, default=0)
    duration_s: Mapped[float | None] = mapped_column(Float)
    # sha256 over the ordered unit hashes: identical for render-identical documents
    normalized_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    minhash: Mapped[bytes | None] = mapped_column(LargeBinary)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary)
    text_chars: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)     # title, author, doc_number, revision, ...
    media: Mapped[dict] = mapped_column(JSON, default=dict)    # probe info, temporal signature
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    files: Mapped[list[File]] = relationship(back_populates="content")
    units: Mapped[list["Unit"]] = relationship(back_populates="content", order_by="Unit.ordinal",
                                               cascade="all, delete-orphan")


class Unit(Base):
    __tablename__ = "units"
    __table_args__ = (UniqueConstraint("content_sha", "kind", "ordinal", name="uq_unit_pos"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    content_sha: Mapped[str] = mapped_column(ForeignKey("contents.sha256"), index=True)
    kind: Mapped[str] = mapped_column(String(16))   # page|slide|frame|segment
    ordinal: Mapped[int] = mapped_column(Integer)
    start_s: Mapped[float | None] = mapped_column(Float)
    end_s: Mapped[float | None] = mapped_column(Float)
    speaker: Mapped[str | None] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text, default="")
    text_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    unit_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    # identity ignoring header/footer bands (page numbers, doc-number footers, dates)
    body_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    phash: Mapped[str | None] = mapped_column(String(16), index=True)
    minhash: Mapped[bytes | None] = mapped_column(LargeBinary)
    embedding: Mapped[bytes | None] = mapped_column(LargeBinary)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    thumb_key: Mapped[str | None] = mapped_column(String(64))
    blob_key: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    content: Mapped[Content] = relationship(back_populates="units")


class SlideBlob(Base):
    """Slide store: one render-equivalent page per unique unit_hash."""
    __tablename__ = "slide_blobs"
    unit_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    blob_key: Mapped[str] = mapped_column(String(64))
    page_index: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # position in a page pack
    size_bytes: Mapped[int] = mapped_column(Integer)                 # apportioned share of the pack
    fmt: Mapped[str] = mapped_column(String(16), default="pdf-page")
    first_content_sha: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)


class LshBand(Base):
    """MinHash LSH bands for sub-quadratic near-duplicate candidate generation."""
    __tablename__ = "lsh_bands"
    band: Mapped[int] = mapped_column(Integer, primary_key=True)
    bucket: Mapped[str] = mapped_column(String(16), primary_key=True)
    content_sha: Mapped[str] = mapped_column(ForeignKey("contents.sha256"), primary_key=True)


class FrameLink(Base):
    """A video keyframe linked to the slide (unit_hash) it shows."""
    __tablename__ = "frame_links"
    frame_unit_id: Mapped[str] = mapped_column(ForeignKey("units.id"), primary_key=True)
    slide_unit_hash: Mapped[str] = mapped_column(String(64), index=True)
    slide_unit_id: Mapped[str] = mapped_column(ForeignKey("units.id"))
    method: Mapped[str] = mapped_column(String(16))   # phash|ocr
    distance: Mapped[float] = mapped_column(Float)


# --------------------------------------------------------------------------- versioning
class Lineage(Base):
    __tablename__ = "lineages"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    key: Mapped[str] = mapped_column(String(300), unique=True)   # docno:... | name:...
    doc_number: Mapped[str | None] = mapped_column(String(100))
    title: Mapped[str | None] = mapped_column(String(500))
    ordering_basis: Mapped[str] = mapped_column(String(32), default="unknown")
    ordering_confidence: Mapped[str] = mapped_column(String(8), default="low")   # high|medium|low
    has_conflict: Mapped[bool] = mapped_column(Boolean, default=False)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    versions: Mapped[list["Version"]] = relationship(back_populates="lineage", order_by="Version.ordinal")


class Version(Base):
    __tablename__ = "versions"
    __table_args__ = (UniqueConstraint("lineage_id", "content_sha", name="uq_version_content"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    lineage_id: Mapped[str] = mapped_column(ForeignKey("lineages.id"), index=True)
    content_sha: Mapped[str] = mapped_column(ForeignKey("contents.sha256"), index=True)
    revision_label: Mapped[str | None] = mapped_column(String(64))
    revision_scheme: Mapped[str | None] = mapped_column(String(16))  # ericsson|rstate|numeric|filename|None
    revision_key: Mapped[list] = mapped_column(JSON, default=list)
    revision_source: Mapped[str | None] = mapped_column(String(32))  # doc_metadata|doc_text|filename|mtime
    released: Mapped[bool | None] = mapped_column(Boolean)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    is_head: Mapped[bool] = mapped_column(Boolean, default=False)
    variant_of_id: Mapped[str | None] = mapped_column(String(32))    # same rendered content as another version
    parent_version_id: Mapped[str | None] = mapped_column(String(32))
    conflict: Mapped[bool] = mapped_column(Boolean, default=False)
    diff_from_parent: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    lineage: Mapped[Lineage] = relationship(back_populates="versions")


# --------------------------------------------------------------------------- relationship graph
class Relation(Base):
    __tablename__ = "relations"
    __table_args__ = (UniqueConstraint("kind", "a_sha", "b_sha", name="uq_relation"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    a_sha: Mapped[str] = mapped_column(String(64), index=True)
    b_sha: Mapped[str] = mapped_column(String(64), index=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)


# --------------------------------------------------------------------------- capsules
class Capsule(Base):
    __tablename__ = "capsules"
    content_sha: Mapped[str] = mapped_column(ForeignKey("contents.sha256"), primary_key=True)
    schema: Mapped[str] = mapped_column(String(32))
    manifest: Mapped[dict] = mapped_column(JSON)
    manifest_hash: Mapped[str] = mapped_column(String(64))
    signature: Mapped[str] = mapped_column(String(64))
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    valid: Mapped[bool] = mapped_column(Boolean, default=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)


class Rendition(Base):
    """Output of the compression decision engine (validated against a quality budget)."""
    __tablename__ = "renditions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    content_sha: Mapped[str] = mapped_column(ForeignKey("contents.sha256"), index=True)
    operation: Mapped[str] = mapped_column(String(64))
    blob_key: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    validation: Mapped[dict] = mapped_column(JSON, default=dict)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)


# --------------------------------------------------------------------------- search
class Posting(Base):
    __tablename__ = "postings"
    term: Mapped[str] = mapped_column(String(64), primary_key=True)
    unit_id: Mapped[str] = mapped_column(ForeignKey("units.id"), primary_key=True)
    tf: Mapped[int] = mapped_column(Integer)


# --------------------------------------------------------------------------- governance
class DeletionPlan(Base):
    __tablename__ = "deletion_plans"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(300))
    scope: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(64))
    # draft|pending_approval|approved|executing|quarantined|purged|restored|rejected|cancelled
    status: Mapped[str] = mapped_column(String(24), default="draft")
    policy_version: Mapped[str] = mapped_column(String(64))
    policy_hash: Mapped[str] = mapped_column(String(64))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    readiness: Mapped[float] = mapped_column(Float, default=0.0)
    required_approvals: Mapped[int] = mapped_column(Integer, default=1)
    approvals: Mapped[list] = mapped_column(JSON, default=list)
    quarantine_until: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    row_version: Mapped[int] = mapped_column(Integer, default=1)

    items: Mapped[list["DeletionItem"]] = relationship(back_populates="plan", cascade="all, delete-orphan")
    __mapper_args__ = {"version_id_col": row_version}


class DeletionItem(Base):
    __tablename__ = "deletion_items"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    plan_id: Mapped[str] = mapped_column(ForeignKey("deletion_plans.id"), index=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id"), index=True)
    survivor_file_id: Mapped[str | None] = mapped_column(ForeignKey("files.id"))
    dup_class: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(32))
    recovery_class: Mapped[str] = mapped_column(String(32))
    victim_sha: Mapped[str] = mapped_column(String(64))
    survivor_sha: Mapped[str | None] = mapped_column(String(64))
    bytes_reclaimable: Mapped[int] = mapped_column(BigInteger, default=0)
    checks: Mapped[list] = mapped_column(JSON, default=list)
    readiness: Mapped[float] = mapped_column(Float, default=0.0)
    deletable: Mapped[bool] = mapped_column(Boolean, default=False)
    # planned|blocked|quarantining|quarantined|purging|purged|restoring|restored|failed
    state: Mapped[str] = mapped_column(String(16), default="planned")
    quarantine_tier: Mapped[str | None] = mapped_column(String(16))
    stub_path: Mapped[str | None] = mapped_column(String(1024))
    error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)

    plan: Mapped[DeletionPlan] = relationship(back_populates="items")


# --------------------------------------------------------------------------- human in the loop
class ReviewTask(Base):
    """A decision KORA will not take on its own: low-confidence inference, privacy
    question, or a data-subject request. Nothing downstream that depends on it
    proceeds until a person with the required role decides."""
    __tablename__ = "review_tasks"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    key: Mapped[str] = mapped_column(String(300), unique=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    subject_type: Mapped[str] = mapped_column(String(32))
    subject_id: Mapped[str] = mapped_column(String(100), index=True)
    question: Mapped[str] = mapped_column(Text)
    options: Mapped[list] = mapped_column(JSON, default=list)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    required_role: Mapped[str] = mapped_column(String(16), default="analyst")
    priority: Mapped[int] = mapped_column(Integer, default=50)
    blocks: Mapped[str | None] = mapped_column(String(200))     # what waits for this decision
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)   # open|decided|dismissed
    decision: Mapped[str | None] = mapped_column(String(100))
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)


# --------------------------------------------------------------------------- jobs
class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "status", "run_after", "priority"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued|running|succeeded|dead|cancelled
    priority: Mapped[int] = mapped_column(Integer, default=100)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    lease_owner: Mapped[str | None] = mapped_column(String(100))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    idempotency_key: Mapped[str | None] = mapped_column(String(300), unique=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=clock.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


# --------------------------------------------------------------------------- audit
class AuditEvent(Base):
    __tablename__ = "audit_events"
    seq: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(64), index=True)
    subject_type: Mapped[str] = mapped_column(String(32))
    subject_id: Mapped[str] = mapped_column(String(100), index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))
    mac: Mapped[str] = mapped_column(String(64))


class AuditHead(Base):
    __tablename__ = "audit_head"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, default=0)
    hash: Mapped[str] = mapped_column(String(64), default="0" * 64)
