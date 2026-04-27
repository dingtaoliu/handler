"""Scheduler channel: in-process cron job execution.

Runs a background loop that checks every 30 seconds for due jobs and
executes them directly. The external watchdog (handler/watchdog/) is
liveness-only: it just checks if the handler is alive and restarts it
if dead.
"""

import asyncio
import logging
import subprocess

from ..environment import Channel
from ..types import Event
from ..event_store import EventStore
from ..utils import next_run_from_now

logger = logging.getLogger("handler.channels.scheduler")


class SchedulerChannel(Channel):
    """Background scheduler: cron job execution."""

    name = "scheduler"

    def __init__(
        self,
        store: EventStore,
        job_check_interval: float = 30.0,
    ):
        self.store = store
        self.job_check_interval = job_check_interval
        self.queue: asyncio.Queue[Event] | None = None

    async def start(self, queue: asyncio.Queue) -> None:
        self.queue = queue
        await self._job_executor_loop()

    async def deliver(self, event: Event, response: str) -> None:
        pass  # fire-and-forget

    # ------------------------------------------------------------------
    # Cron job execution
    # ------------------------------------------------------------------

    async def _job_executor_loop(self) -> None:
        """Check for due cron jobs every 30 seconds and execute them."""
        while True:
            await asyncio.sleep(self.job_check_interval)
            try:
                await self._run_due_jobs()
            except Exception as e:
                logger.error(f"job executor failed: {e}", exc_info=True)

    async def _run_due_jobs(self) -> None:
        jobs = self.store.get_due_jobs()
        if not jobs:
            return

        for job in jobs:
            logger.info(f"[job #{job['id']}] {job['name']} (type={job['type']})")
            try:
                if job["type"] == "shell":
                    await self._run_shell_job(job)
                elif job["type"] == "prompt":
                    await self._run_prompt_job(job)
                else:
                    logger.warning(f"unknown job type '{job['type']}', skipping")
                    continue
            except Exception as e:
                logger.error(f"[job #{job['id']}] failed: {e}")

            # Advance or delete
            if job.get("one_shot"):
                self.store.delete_cron_job(job["id"])
                logger.info(f"[job #{job['id']}] deleted (one_shot)")
            else:
                # Always advance recurring jobs to avoid hammering on error
                try:
                    self.store.update_job_run(
                        job["id"], next_run_from_now(job["schedule"])
                    )
                except ValueError as e:
                    logger.error(f"[job #{job['id']}] bad schedule: {e}")

    async def _run_shell_job(self, job: dict) -> None:
        """Execute a shell job in a subprocess (run in executor to not block)."""
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(
                job["payload"],
                shell=True,
                capture_output=True,
                text=True,
                timeout=120,
            ),
        )
        preview = (result.stdout or result.stderr)[:300]
        logger.info(f"[job #{job['id']}] shell exit={result.returncode}: {preview}")

    async def _run_prompt_job(self, job: dict) -> None:
        """Push a prompt job directly onto the event queue."""
        cid = job["conversation_id"] or "web"
        notify = job.get("notify_channel", "")
        queue = self.queue
        if queue is None:
            raise RuntimeError("scheduler queue not initialized")
        await queue.put(
            Event(
                type="cron_prompt",
                source="scheduler",
                conversation_id=cid,
                user_id=job.get("user_id"),
                data={
                    "content": job["payload"],
                    "cron_job_name": job["name"],
                    "notify_channel": notify,
                },
            )
        )
        logger.info(
            f"[job #{job['id']}] prompt pushed to queue (conversation={cid}, user={job.get('user_id') or ''}, notify={notify or 'none'})"
        )
