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
    Approval,
    ApprovalStatus,
    AuditEntry,
    Connection,
    Event,
    MemoryTopic,
    Schedule,
    SettingsVersion,
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

    async def list_events(
        self, *, source: str | None = None, status: str | None = None, limit: int = 100,
    ) -> list[tuple[Event, str]]:
        """Recent events with their status (routed|ignored), newest first."""
        ...


class Store(EventQueue, Protocol):
    # --- threads ---
    async def get_thread(self, key: str) -> Thread | None: ...

    async def thread_metadata(self, keys: list[str], user_id: str | None) -> dict[str, dict]: ...

    async def set_thread_title(self, key: str, title: str, *, manual: bool = True) -> bool: ...

    async def mark_thread_read(self, key: str, user_id: str, last_message_at: datetime | None): ...

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

    async def abandon_run(self, run_id: str, error: str) -> bool:
        """Mark a still-running run abandoned (its worker gave up or lost the
        lease). Not fenced: it only changes a row that is still 'running'."""
        ...

    async def sweep_abandoned_runs(self, thread_key: str | None = None) -> list[str]:
        """Mark abandoned every 'running' run (of one thread, or all) whose
        lease is no longer live. Returns their ids."""
        ...

    async def revive_thread(self, key: str, *, reset_session: bool = False,
                            discard_pending: bool = False) -> bool:
        """Clear dead/attempts/backoff so the thread is claimable again.

        reset_session clears session_id (the next turn starts a fresh harness
        session from a transcript recap); discard_pending marks unconsumed
        events consumed so they are not run again. False if no such thread.
        """
        ...

    async def get_run(self, run_id: str) -> Run | None: ...

    async def list_runs(self, thread_key: str, limit: int = 50) -> list[Run]: ...

    # --- messages (the thread's official record) ---
    async def add_messages(self, lease: Lease, messages: list[Message]) -> bool:
        """Append messages atomically. Fenced."""
        ...

    async def list_messages(
        self, thread_key: str, limit: int = 200, before: datetime | None = None,
        *, newest: bool = False,
    ) -> list[Message]:
        """Messages in chronological order; newest selects the most recent window."""
        ...

    # --- run steps (observability; append-only, not fenced) ---
    async def add_step(self, step: RunStep) -> None: ...

    async def update_step(self, step: RunStep) -> None:
        """Update ended_at/status/usage/cost/data of an existing (run_id, seq)."""
        ...

    async def list_steps(self, run_id: str) -> list[RunStep]: ...

    async def close(self) -> None: ...


# --------------------------------------------------------------------------
# Phase 2 domain stores. Each is implemented as a mixin on SQLiteCore
# (store/sqlite_<domain>.py) and later on the Postgres store.
# --------------------------------------------------------------------------


class ScheduleStore(Protocol):
    async def create_schedule(self, schedule: Schedule) -> None: ...
    async def get_schedule(self, schedule_id: str) -> Schedule | None: ...
    async def list_schedules(self, include_disabled: bool = False) -> list[Schedule]: ...
    async def update_schedule(self, schedule: Schedule) -> None: ...
    async def delete_schedule(self, schedule_id: str) -> bool: ...

    async def next_due_at(self) -> datetime | None:
        """Earliest next_run_at among enabled schedules (None if none)."""
        ...

    async def due_schedules(self, now: datetime, limit: int = 50) -> list[Schedule]:
        """Enabled schedules with next_run_at <= now, oldest first."""
        ...

    async def fire_schedule(
        self,
        schedule: Schedule,
        expected_next_run_at: datetime,
        next_run_at: datetime | None,
        event: Event,
        thread: ThreadSpec,
    ) -> bool:
        """Atomically emit the schedule's event and advance it.

        In one transaction: only if the stored next_run_at still equals
        expected_next_run_at (another worker hasn't fired it), put the event
        exactly like EventQueue.put (idempotent on its key, routes the
        thread), set last_run_at = expected_next_run_at and next_run_at; if
        next_run_at is None, set enabled = False. Returns True if this call
        fired it. Wakes waiting readers.
        """
        ...


class ApprovalStore(Protocol):
    async def create_approval(self, approval: Approval) -> None: ...
    async def get_approval(self, approval_id: str) -> Approval | None: ...
    async def list_approvals(
        self,
        status: ApprovalStatus | None = None,
        limit: int = 100,
        thread_key: str | None = None,
    ) -> list[Approval]: ...

    async def transition_approval(
        self,
        approval_id: str,
        expected: ApprovalStatus,
        new: ApprovalStatus,
        *,
        decided_by: str | None = None,
        result: dict | None = None,
    ) -> bool:
        """Compare-and-set the status. Sets decided_by/decided_at when given a
        decider, and result when given. Returns False if the status wasn't
        `expected` (e.g. already decided)."""
        ...


class AuditStore(Protocol):
    async def add_audit(self, entry: AuditEntry) -> None: ...
    async def list_audit(
        self, run_id: str | None = None, limit: int = 200
    ) -> list[AuditEntry]:
        """Newest first."""
        ...


class MemoryStore(Protocol):
    async def list_topics(self) -> list[MemoryTopic]:
        """All topics, by name (content included)."""
        ...

    async def get_topic(self, name: str) -> MemoryTopic | None: ...

    async def put_topic(self, topic: MemoryTopic, expected_version: int | None) -> MemoryTopic | None:
        """Create (expected_version None: must not exist) or replace
        (expected_version must equal the stored version). Stores
        version = expected_version + 1 (or 1). Returns the stored topic, or
        None on a version conflict."""
        ...

    async def delete_topic(self, name: str) -> bool: ...


class SettingsStore(Protocol):
    async def get_settings(self, section: str) -> SettingsVersion | None:
        """Latest version of a section."""
        ...

    async def put_settings(
        self, section: str, value: dict, *, updated_by: str | None,
        expected_version: int | None, schema_version: int = 1,
    ) -> SettingsVersion | None:
        """Append a new version (expected_version None: section must not
        exist yet; otherwise must equal the latest). Returns the new version,
        or None on conflict. History is never overwritten."""
        ...

    async def settings_history(self, section: str, limit: int = 50) -> list[SettingsVersion]:
        """Newest first."""
        ...


class ConnectionStore(Protocol):
    async def put_connection(self, connection: Connection) -> None:
        """Insert or replace by id."""
        ...

    async def get_connection(self, connection_id: str) -> Connection | None: ...

    async def find_connection(self, connector: str, owner: str) -> Connection | None:
        """The active connection for (connector, owner), if any."""
        ...

    async def list_connections(self, owner: str | None = None) -> list[Connection]: ...

    async def delete_connection(self, connection_id: str) -> bool:
        """Remove a connection record; False when it did not exist."""
        ...


class SecretStore(Protocol):
    """Opaque secret values by key (connections point at them via secret_ref).

    The store keeps whatever string it is given; callers encrypt first
    (gateway/secret_box.py) so the database never holds plaintext tokens.
    """

    async def put_secret(self, key: str, value: str) -> None: ...

    async def get_secret(self, key: str) -> str | None: ...

    async def delete_secret(self, key: str) -> bool: ...
