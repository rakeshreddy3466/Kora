"""Worker loop: claim -> run handler (with heartbeats) -> complete/fail.

Graceful shutdown on SIGTERM/SIGINT: finish the current job, stop claiming.
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import traceback
from typing import Callable

from ..logs import correlation_id
from . import queue as q

log = logging.getLogger(__name__)

Handler = Callable[[q.JobContext], None]
registry: dict[str, Handler] = {}


def handler(kind: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        registry[kind] = fn
        return fn
    return deco


class Worker:
    def __init__(self, worker_id: str | None = None, kinds: list[str] | None = None, lease_s: int = 120):
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
        self.kinds = kinds
        self.lease_s = lease_s
        self._stop = threading.Event()

    def stop(self, *_a) -> None:
        log.info("worker stopping after current job")
        self._stop.set()

    def _run(self, job: q.ClaimedJob) -> str:
        fn = registry.get(job.kind)
        if fn is None:
            q.fail(job, f"no handler for {job.kind}", permanent=True)
            return "dead"
        ctx = q.JobContext(job, self.lease_s)
        hb_stop = threading.Event()

        def _beat():
            while not hb_stop.wait(max(1.0, self.lease_s / 3)):
                if not q.heartbeat(job.id, job.token, self.lease_s):
                    log.warning("lease lost", extra={"job_id": job.id})
                    return

        hb = threading.Thread(target=_beat, daemon=True)
        hb.start()
        token = correlation_id.set(f"job:{job.id}")
        t0 = time.monotonic()
        try:
            fn(ctx)
            if not ctx.completed:
                with ctx.tx():
                    pass
            log.info("job done", extra={"job_id": job.id, "job_kind": job.kind,
                                        "duration_ms": int((time.monotonic() - t0) * 1000)})
            return "succeeded"
        except q.LeaseLost:
            log.warning("job lease lost; work rolled back", extra={"job_id": job.id})
            return "lease_lost"
        except q.PermanentError as e:
            log.error("job failed permanently: %s", e, extra={"job_id": job.id, "job_kind": job.kind})
            return q.fail(job, f"PermanentError: {e}", permanent=True)
        except Exception as e:  # retryable
            log.exception("job failed", extra={"job_id": job.id, "job_kind": job.kind})
            return q.fail(job, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=8)}")
        finally:
            hb_stop.set()
            correlation_id.reset(token)

    def run_once(self) -> bool:
        job = q.claim(self.worker_id, self.kinds, self.lease_s)
        if job is None:
            return False
        self._run(job)
        return True

    def drain(self, max_jobs: int = 100_000) -> int:
        """Process until no claimable job remains (delayed retries are not waited for)."""
        n = 0
        while n < max_jobs and self.run_once():
            n += 1
        return n

    def run_forever(self, poll_s: float = 1.0) -> None:
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        log.info("worker %s started", self.worker_id)
        while not self._stop.is_set():
            if not self.run_once():
                self._stop.wait(poll_s)
