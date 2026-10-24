"""In-memory job registry with a small pub/sub used by the SSE endpoint.

Async task flow (enqueue -> progress updates -> fetch result), in-process:
no Redis/RQ needed for a single-user local tool. Jobs run in worker threads;
every engine event is kept (capped) for late subscribers and pushed live to
SSE subscribers.
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

# Events that are NOT replayed to late subscribers (too chatty / too large).
_VOLATILE_EVENTS = frozenset({"stream_chunk"})
_EVENT_REPLAY_CAP = 2000


@dataclass
class PageRecord:
    """One page of a converted document (for the review UI)."""

    page: int
    text: str = ""
    image_b64: str = ""  # PNG, base64-encoded thumbnail (may be empty)


class JobCancelled(Exception):
    """Raised by engines when the user cancels a running job."""


class Job:
    def __init__(self, source_desc: str, engine: str, detail: dict[str, Any]):
        self.id = uuid.uuid4().hex[:12]
        self.source_desc = source_desc
        self.engine = engine  # resolved engine: "ollama"
        self.detail = detail  # e.g. {"model": ..., "dpi": ...}
        self.status = "queued"  # queued | running | done | error | cancelled
        # Set by cancel(); engines check it between pages and the worker
        # honours it so a stuck job can be stopped (frees its slot).
        self.cancel_event = threading.Event()
        self.error: str | None = None
        self.created_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None

        self.pages: list[PageRecord] = []
        self.total_pages: int | None = None
        self.current_page: int = 0
        self.phase: str = ""  # human-readable phase, e.g. "Rendering pages"
        self.result: dict[str, Any] | None = None  # final outputs

        self._events: deque[tuple[str, dict]] = deque(maxlen=_EVENT_REPLAY_CAP)
        self._subscribers: set[queue.Queue] = set()
        self._lock = threading.Lock()

    # -- pub/sub ----------------------------------------------------------
    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def emit(self, name: str, data: dict[str, Any]) -> None:
        """Publish an event to live subscribers and (if stable) history."""
        if name not in _VOLATILE_EVENTS:
            with self._lock:
                self._events.append((name, data))
        payload = {"event": name, "data": data}
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(payload)
            except queue.Full:  # slow consumer; drop rather than block worker
                pass

    def history(self) -> list[tuple[str, dict]]:
        with self._lock:
            return list(self._events)

    @property
    def terminal(self) -> bool:
        return self.status in ("done", "error", "cancelled")

    def cancel(self) -> bool:
        """Request cancellation. Returns True if the job was still active."""
        with self._lock:
            if self.terminal:
                return False
            self.cancel_event.set()
            return True

    def snapshot(self) -> dict[str, Any]:
        """JSON-safe status summary for GET /api/jobs/{id}."""
        return {
            "job_id": self.id,
            "status": self.status,
            "engine": self.engine,
            "source": self.source_desc,
            "detail": self.detail,
            "phase": self.phase,
            "current_page": self.current_page,
            "total_pages": self.total_pages,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "processing_time": (
                round(self.finished_at - (self.started_at or self.created_at), 2)
                if self.finished_at and self.started_at
                else None
            ),
            "has_result": self.result is not None,
        }


class TaskRegistry:
    """Holds jobs, runs their worker threads, prunes old results."""

    def __init__(self, retention: int, max_concurrent: int):
        self.jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self.retention = retention
        self._semaphore = threading.BoundedSemaphore(max_concurrent)
        self._lock = threading.Lock()

    def create(self, source_desc: str, engine: str, detail: dict[str, Any]) -> Job:
        job = Job(source_desc, engine, detail)
        with self._lock:
            self.jobs[job.id] = job
            self._order.append(job.id)
            self._prune_locked()
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> Job | None:
        """Request cancellation of a job. Returns the job, or None if unknown."""
        job = self.jobs.get(job_id)
        if job is not None:
            job.cancel()
        return job

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(reversed(self._order))
        return [self.jobs[i].snapshot() for i in ids if i in self.jobs]

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self.jobs.pop(job_id, None)
            if job is not None:
                try:
                    self._order.remove(job_id)
                except ValueError:
                    pass
        return job is not None

    def start_worker(self, job: Job, fn: Callable[[Job], None]) -> None:
        """Run fn(job) in a worker thread, guarded by the concurrency limit."""

        def _run() -> None:
            self._semaphore.acquire()
            try:
                if job.cancel_event.is_set():  # cancelled while queued
                    job.status = "cancelled"
                    job.finished_at = time.time()
                    return
                job.status = "running"
                job.started_at = time.time()
                job.emit("status", {"status": "running", "phase": job.phase or "Starting"})
                fn(job)
                if job.status == "running":  # engine didn't mark done/error itself
                    job.status = "done"
                job.finished_at = time.time()
            except JobCancelled:
                job.status = "cancelled"
                job.error = None
                job.finished_at = time.time()
            except Exception as exc:  # noqa: BLE001 - surface any failure to the UI
                if job.cancel_event.is_set():
                    # A transport error caused by tearing down a cancelled
                    # job is not a real failure.
                    job.status = "cancelled"
                    job.error = None
                else:
                    job.status = "error"
                    job.error = str(exc) or exc.__class__.__name__
                job.finished_at = time.time()
            finally:
                self._semaphore.release()
                # Results live in memory; drop the source files on disk.
                scratch = getattr(job, "scratch_dir", None)
                if scratch and job.terminal:
                    import shutil

                    shutil.rmtree(scratch, ignore_errors=True)
                phase = {"done": "", "cancelled": "Cancelled"}.get(job.status, "Failed")
                job.emit(
                    "status",
                    {
                        "status": job.status,
                        "phase": phase,
                        "error": job.error,
                    },
                )

        t = threading.Thread(target=_run, name=f"job-{job.id}", daemon=True)
        t.start()

    def _prune_locked(self) -> None:
        while len(self._order) > self.retention:
            oldest = self._order.pop(0)
            old = self.jobs.pop(oldest, None)
            if old is not None and old.terminal:
                # Drop heavy payloads of finished jobs to bound memory.
                old.pages.clear()
                old.result = None
