"""Lightweight in-process interval scheduler.

A single asyncio background loop fires registered jobs on their interval. Each
job's config (enabled, interval) is read live every tick through callables, so
an operator changing a schedule in Settings takes effect on the next tick with
no restart. A job that raises is caught and recorded, never crashing the loop or
another job. Started and stopped from the app lifespan.

This is deliberately not APScheduler/cron: AgeniusDesk needs "every N hours,
best-effort, survive restarts" for a handful of built-in maintenance jobs
(backups, and later health reports), not calendar-precise scheduling. Keeping it
a ~120-line dependency-free service keeps the default image lean.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Loop granularity. Jobs fire at most this coarsely; intervals are hours, so a
# 30s tick is plenty precise and keeps the loop nearly idle.
TICK_SECONDS = 30.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Job:
    id: str
    func: Callable[[], Awaitable]
    interval_fn: Callable[[], float]  # current interval in seconds (read live)
    enabled_fn: Callable[[], bool]    # whether the job is active (read live)
    # Monotonic deadline for the next run; None means "not scheduled yet".
    next_run: Optional[float] = None
    running: bool = False
    last_run_at: Optional[str] = None  # ISO wall-clock of the last completion
    last_status: str = ""              # "ok" | "error" | ""
    last_error: str = ""
    last_duration_ms: Optional[float] = None
    last_result: Optional[dict] = None  # small summary the job returns


class Scheduler:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None

    def register(
        self,
        job_id: str,
        func: Callable[[], Awaitable],
        interval_fn: Callable[[], float],
        enabled_fn: Callable[[], bool],
    ) -> None:
        """Register (or replace) a job. Idempotent, so re-registering on a hot
        reload keeps the loop's single copy."""
        self._jobs[job_id] = Job(id=job_id, func=func, interval_fn=interval_fn, enabled_fn=enabled_fn)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())
        logger.info("scheduler started (%d job(s))", len(self._jobs))

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            except Exception as e:  # noqa: BLE001
                logger.debug("scheduler stop: %s", e)
        self._task = None

    def status(self) -> list[dict]:
        """Per-job status for the settings UI, without leaking the callables."""
        now = time.monotonic()
        out = []
        for j in self._jobs.values():
            secs = None
            if j.enabled_fn() and j.next_run is not None:
                secs = max(0, round(j.next_run - now))
            out.append({
                "id": j.id,
                "enabled": j.enabled_fn(),
                "interval_seconds": round(j.interval_fn()),
                "running": j.running,
                "next_run_in_seconds": secs,
                "last_run_at": j.last_run_at,
                "last_status": j.last_status,
                "last_error": j.last_error,
                "last_duration_ms": j.last_duration_ms,
                "last_result": j.last_result,
            })
        return out

    async def run_now(self, job_id: str) -> dict:
        """Fire a job immediately (manual "run now"), bypassing the schedule but
        respecting the single-flight guard. Awaits completion and returns its
        recorded status. Reschedules the next automatic run from now."""
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.running:
            return {"id": job_id, "skipped": "already running"}
        await self._fire(job)
        return {
            "id": job_id,
            "last_status": job.last_status,
            "last_error": job.last_error,
            "last_result": job.last_result,
        }

    async def _run(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            now = time.monotonic()
            for job in list(self._jobs.values()):
                if job.running or not job.enabled_fn():
                    continue
                if job.next_run is None:
                    # First scheduling for this job: wait one full interval before
                    # the first run so a restart loop never hammers backups on boot.
                    job.next_run = now + max(TICK_SECONDS, job.interval_fn())
                    continue
                if now >= job.next_run:
                    job.next_run = now + max(TICK_SECONDS, job.interval_fn())
                    asyncio.create_task(self._fire(job))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _fire(self, job: Job) -> None:
        job.running = True
        started = time.monotonic()
        try:
            result = await job.func()
            job.last_status = "ok"
            job.last_error = ""
            job.last_result = result if isinstance(result, dict) else None
        except Exception as e:  # noqa: BLE001 - a failing job must not crash the loop
            job.last_status = "error"
            job.last_error = str(e)[:300]
            job.last_result = None
            logger.exception("scheduled job %s failed: %s", job.id, e)
        finally:
            job.running = False
            job.last_run_at = _now_iso()
            job.last_duration_ms = round((time.monotonic() - started) * 1000, 1)
            # Reschedule relative to completion so a long run doesn't stack up.
            job.next_run = time.monotonic() + max(TICK_SECONDS, job.interval_fn())


# Process-wide singleton, mirroring backend.websocket.manager.
scheduler = Scheduler()
