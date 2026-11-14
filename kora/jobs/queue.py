"""Durable job queue stored in the primary database.

Why not a separate broker for the core pipeline? Enqueueing in the *same
transaction* as the domain write that caused it (transactional outbox) removes
the dual-write problem: a file is never marked "hashed" without its extract
job existing, and vice versa. Celery/RabbitMQ can be put behind this interface
later for fan-out at scale.

Semantics
  * at-least-once delivery; handlers are idempotent (content-addressed writes,
    upserts, unique constraints) so redelivery is safe
  * idempotency keys de-duplicate enqueues (e.g. one extract per content hash)
  * leases with fencing tokens: a worker whose lease expired cannot complete
    or fail a job that was re-claimed by someone else
  * exponential backoff with jitter, max attempts, then dead-letter ("dead")
  * PostgreSQL claims use FOR UPDATE SKIP LOCKED; SQLite serialises writers
"""
from __future__ import annotations

import contextlib
import random
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterator

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import clock, db
from ..models import Job

BACKOFF_BASE_S = 5
BACKOFF_CAP_S = 3600


class PermanentError(Exception):
    """Non-retryable failure (corrupt/unsupported input, policy violation)."""


class LeaseLost(Exception):
    """The job was re-claimed by another worker; our work must be rolled back."""


@dataclass(frozen=True)
class ClaimedJob:
    id: str
    kind: str
    payload: dict
    attempts: int
    max_attempts: int
    token: str


def enqueue(s: Session, kind: str, payload: dict, *, key: str | None = None, priority: int = 100,
            delay_s: float = 0, max_attempts: int = 5) -> str:
    """Enqueue inside the caller's transaction. Returns the (possibly existing) job id."""
    if key:
        existing = s.scalar(select(Job.id).where(Job.idempotency_key == key))
        if existing:
            return existing
    job = Job(kind=kind, payload=payload, idempotency_key=key, priority=priority,
              run_after=clock.now() + timedelta(seconds=delay_s), max_attempts=max_attempts)
    try:
        with s.begin_nested():
            s.add(job)
            s.flush()
    except IntegrityError:
        # concurrent enqueue with the same key won the race
        existing = s.scalar(select(Job.id).where(Job.idempotency_key == key))
        if existing:
            return existing
        raise
    return job.id


def claim(worker_id: str, kinds: list[str] | None = None, lease_s: int = 120) -> ClaimedJob | None:
    for _ in range(50):           # skip over jobs we dead-letter while claiming
        now = clock.now()
        with db.session_scope() as s:
            q = select(Job).where(or_(
                and_(Job.status == "queued", Job.run_after <= now),
                and_(Job.status == "running", Job.lease_expires_at < now),     # crashed worker
            )).order_by(Job.priority, Job.run_after, Job.created_at).limit(1)
            if kinds:
                q = q.where(Job.kind.in_(kinds))
            if db.is_postgres():
                q = q.with_for_update(skip_locked=True)
            job = s.scalars(q).first()
            if job is None:
                return None
            if job.status == "running" and job.attempts >= job.max_attempts:
                job.status = "dead"
                job.last_error = (job.last_error or "") + "\nlease expired on final attempt"
                job.lease_owner = None
                job.updated_at = job.finished_at = now
                continue
            token = f"{worker_id}:{secrets.token_hex(8)}"
            job.status = "running"
            job.attempts += 1
            job.lease_owner = token
            job.lease_expires_at = now + timedelta(seconds=lease_s)
            job.updated_at = now
            return ClaimedJob(job.id, job.kind, dict(job.payload or {}), job.attempts, job.max_attempts, token)
    return None


def release_orphans(prefix: str) -> int:
    """Make jobs claimable at once when the local process that held them is gone.

    Only for a single-machine setup (the dashboard on SQLite): worker ids there look like
    "<prefix><n>-<pid>". A job whose pid no longer exists cannot be finishing, so waiting out its
    lease only delays the user. The fencing token still guards against a late write."""
    import os
    import re
    if db.is_postgres():
        return 0
    pat = re.compile(rf"^{re.escape(prefix)}\d+-(\d+):")
    freed = 0
    with db.session_scope() as s:
        for job in s.scalars(select(Job).where(Job.status == "running")):
            m = pat.match(job.lease_owner or "")
            if not m or int(m.group(1)) == os.getpid():
                continue
            try:
                os.kill(int(m.group(1)), 0)
                continue                              # still alive (maybe another dashboard)
            except ProcessLookupError:
                pass
            except PermissionError:
                continue
            job.lease_expires_at = clock.now() - timedelta(seconds=1)
            freed += 1
    return freed


def heartbeat(job_id: str, token: str, lease_s: int = 120) -> bool:
    with db.session_scope() as s:
        res = s.execute(update(Job).where(Job.id == job_id, Job.status == "running", Job.lease_owner == token)
                        .values(lease_expires_at=clock.now() + timedelta(seconds=lease_s)))
        return res.rowcount == 1


def set_progress(job_id: str, token: str, progress: dict) -> None:
    """Record where a running job is (best effort; only while this worker still holds it)."""
    try:
        with db.session_scope() as s:
            s.execute(update(Job).where(Job.id == job_id, Job.status == "running", Job.lease_owner == token)
                      .values(result={"progress": progress}))
    except Exception:
        pass


def complete_in(s: Session, job_id: str, token: str, result: dict | None = None) -> None:
    """Mark complete inside the handler's transaction (fenced). Raises LeaseLost."""
    res = s.execute(update(Job).where(Job.id == job_id, Job.status == "running", Job.lease_owner == token)
                    .values(status="succeeded", result=result or {}, lease_owner=None,
                            finished_at=clock.now(), updated_at=clock.now()))
    if res.rowcount != 1:
        raise LeaseLost(job_id)


def _backoff(attempt: int) -> float:
    ceiling = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** max(0, attempt - 1)))
    return ceiling / 2 + random.uniform(0, ceiling / 2)      # "equal jitter"


def fail(job: ClaimedJob, error: str, permanent: bool = False) -> str:
    with db.session_scope() as s:
        row = s.get(Job, job.id)
        if row is None or row.status != "running" or row.lease_owner != job.token:
            return "lease_lost"
        now = clock.now()
        row.last_error = error[-4000:]
        row.lease_owner = None
        row.updated_at = now
        if permanent or row.attempts >= row.max_attempts:
            row.status = "dead"
            row.finished_at = now
        else:
            row.status = "queued"
            row.run_after = now + timedelta(seconds=_backoff(row.attempts))
        return row.status


def retry_dead(job_id: str) -> bool:
    with db.session_scope() as s:
        row = s.get(Job, job_id)
        if row is None or row.status != "dead":
            return False
        row.status, row.attempts, row.run_after = "queued", 0, clock.now()
        row.updated_at = clock.now()
        return True


class JobContext:
    """Passed to handlers. Use ``with ctx.tx() as s:`` for the final write:
    the job is completed (fenced) in the same transaction, so domain writes,
    follow-up enqueues and completion commit atomically or not at all."""

    def __init__(self, job: ClaimedJob, lease_s: int):
        self.job = job
        self.lease_s = lease_s
        self.completed = False
        self.result: dict = {}

    @property
    def payload(self) -> dict:
        return self.job.payload

    def heartbeat(self) -> None:
        if not heartbeat(self.job.id, self.job.token, self.lease_s):
            raise LeaseLost(self.job.id)

    @contextlib.contextmanager
    def keepalive(self, every_s: float | None = None, progress_dir=None) -> Iterator[None]:
        """Renew the lease from a side thread while a long blocking step runs (transcodes,
        ASR). If renewal fails the lease is gone; the fenced completion will then refuse.
        With progress_dir (a folder, or a function returning one once it exists), the sandbox's
        progress note is copied to the job for the dashboard."""
        import threading
        stop = threading.Event()
        period = every_s or max(5.0, self.lease_s / 3)
        tick = 2.0 if progress_dir else period
        last = [0.0]

        def beat():
            import time as _t
            while not stop.wait(tick):
                d = progress_dir() if callable(progress_dir) else progress_dir
                if d is not None:
                    from ..extraction.progress import read
                    pr = read(d)
                    if pr:
                        set_progress(self.job.id, self.job.token, pr)
                if _t.monotonic() - last[0] >= period:
                    last[0] = _t.monotonic()
                    if not heartbeat(self.job.id, self.job.token, self.lease_s):
                        return
        t = threading.Thread(target=beat, name=f"keepalive-{self.job.id}", daemon=True)
        t.start()
        try:
            yield
        finally:
            stop.set()
            t.join(timeout=5)

    @contextlib.contextmanager
    def tx(self) -> Iterator[Session]:
        with db.session_scope() as s:
            yield s
            complete_in(s, self.job.id, self.job.token, self.result)
        self.completed = True
