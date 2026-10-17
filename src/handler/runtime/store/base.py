"""Storage contracts. Implementations: SQLite (default), Postgres (later).

All methods are async. Implementations must be safe to call concurrently from
many asyncio tasks in one process. Writes made on behalf of a run are fenced
on the thread's lease version: if the caller no longer holds the lease, the
write is rejected (returns False) and nothing is changed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ..types import (
    Event,
    Lane,
    Lease,
    Message,
    Run,
    RunStep,
    Thread,
    ThreadSpec,
)


class EventQueue(Protocol):
    async def put(self, event: Event, thread: ThreadSpec | None) -> bool:
        """Durably store an event and, if `thread` is given, route it.

        In one transaction: insert the event (no-op returning False if its
        idempotency_key already exists); if `thread` is given, create the
        thread from the spec when missing and mark it pending
        (pending_since = COALESCE(pending_since, now)); otherwise store the
        event with status 'ignored'. Then wake waiting readers.
        Returns True if the event was newly stored.
        """
        ...

    async def claim(
        self, owner: str, lane: Lane, limit: int, lease_seconds: float
    ) -> list[Lease]:
        """Lease up to `limit` threads in `lane` that have pending work.

        Eligible: pending_since set, not dead, available_at <= now (or null),
        and no live lease (lease_until null or < now). Ordered by priority,
        then pending_since. Each claim increments lease_version.
        """
        ...

    async def heartbeat(self, lease: Lease, lease_seconds: float) -> Lease | None:
        """Extend a lease. Returns the new lease, or None if it was lost."""
        ...

    async def pending_events(self, thread_key: str) -> list[Event]:
        """Routed events on the thread not yet consumed by a run, oldest first."""
        ...

    async def complete(self, lease: Lease, run_id: str, event_ids: list[str]) -> bool:
        """Mark events consumed by run_id and release the lease.

        The thread stays pending if unconsumed events remain (new arrivals
        during the run); otherwise pending_since is cleared. Resets attempts.
        Fenced on lease.version.
        """
        ...

    async def fail(
        self, lease: Lease, error: str, retry_in_seconds: float | None
    ) -> bool:
        """Release the lease after a failed run.

        attempts += 1, last_error = error. If retry_in_seconds is None the
        thread is marked dead; otherwise available_at = now + retry_in_seconds.
        Events stay unconsumed. Fenced on lease.version.
        """
        ...

    async def wait_for_work(self, timeout: float) -> None:
        """Return when new work may be available, or after `timeout` seconds."""
        ...


class Store(EventQueue, Protocol):
    # --- threads ---
    async def get_thread(self, key: str) -> Thread | None: ...

    async def list_threads(
        self, owner_user_id: str | None = None, limit: int = 100
    ) -> list[Thread]: ...

    async def set_session(self, lease: Lease, session_id: str) -> bool:
        """Record the harness session id for the thread. Fenced."""
        ...

    # --- runs ---
    async def create_run(self, lease: Lease, run: Run) -> bool:
        """Insert a run row (status running). Fenced."""
        ...

    async def finish_run(self, lease: Lease, run: Run) -> bool:
        """Update status, finished_at, error, usage, cost_usd. Fenced."""
        ...

    async def get_run(self, run_id: str) -> Run | None: ...

    async def list_runs(self, thread_key: str, limit: int = 50) -> list[Run]: ...

    # --- messages (the thread's official record) ---
    async def add_messages(self, lease: Lease, messages: list[Message]) -> bool:
        """Append messages atomically. Fenced."""
        ...

    async def list_messages(
        self, thread_key: str, limit: int = 200, before: datetime | None = None
    ) -> list[Message]:
        """Messages oldest first."""
        ...

    # --- run steps (observability; append-only, not fenced) ---
    async def add_step(self, step: RunStep) -> None: ...

    async def update_step(self, step: RunStep) -> None:
        """Update ended_at/status/usage/cost/data of an existing (run_id, seq)."""
        ...

    async def list_steps(self, run_id: str) -> list[RunStep]: ...

    async def close(self) -> None: ...
