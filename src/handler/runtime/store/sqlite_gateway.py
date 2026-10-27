"""SQLite storage for approvals and the audit log (mixins for SQLiteStore)."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import fields
from datetime import datetime
from typing import Any

from ..types import Approval, ApprovalStatus, AuditEntry, Decision
from .sqlite import _insert, _timestamp, _transaction, _values

_JSON = {"args", "result"}
_TIMES = {"decided_at", "expires_at", "created_at", "at"}


def _row_values(record: Any) -> dict[str, Any]:
    values = _values(record)  # times and enums; JSON handled below
    for name in _JSON:
        if name in values and values[name] is not None:
            values[name] = json.dumps(record.__dict__[name])
    for name in _TIMES:
        if isinstance(values.get(name), datetime):
            values[name] = _timestamp(values[name])
    return values


def _from_row(cls: type, row: sqlite3.Row):
    values = {f.name: row[f.name] for f in fields(cls)}
    for name, value in values.items():
        if value is None:
            continue
        if name in _JSON:
            values[name] = json.loads(value)
        elif name in _TIMES:
            values[name] = datetime.fromisoformat(value)
    if cls is Approval:
        values["status"] = ApprovalStatus(values["status"])
    else:
        values["decision"] = Decision(values["decision"])
    return cls(**values)


class ApprovalMixin:
    async def create_approval(self, approval: Approval) -> None:
        def write(db: sqlite3.Connection) -> None:
            _insert(db, "approvals", _row_values(approval))

        await self._call(write)

    async def get_approval(self, approval_id: str) -> Approval | None:
        def read(db: sqlite3.Connection) -> Approval | None:
            row = db.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            return _from_row(Approval, row) if row else None

        return await self._call(read)

    async def list_approvals(
        self,
        status: ApprovalStatus | None = None,
        limit: int = 100,
        thread_key: str | None = None,
    ) -> list[Approval]:
        def read(db: sqlite3.Connection) -> list[Approval]:
            clauses, params = [], []
            if status:
                clauses.append("status = ?")
                params.append(status.value)
            if thread_key:
                clauses.append("thread_key = ?")
                params.append(thread_key)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            rows = db.execute(
                f"SELECT * FROM approvals {where} ORDER BY created_at DESC, rowid DESC "
                "LIMIT ?", (*params, limit),
            )
            return [_from_row(Approval, row) for row in rows]

        return await self._call(read)

    async def transition_approval(
        self,
        approval_id: str,
        expected: ApprovalStatus,
        new: ApprovalStatus,
        *,
        decided_by: str | None = None,
        result: dict | None = None,
    ) -> bool:
        def write(db: sqlite3.Connection) -> bool:
            sets = ["status = ?"]
            params: list[Any] = [new.value]
            if decided_by is not None:
                sets += ["decided_by = ?", "decided_at = ?"]
                params += [decided_by, _timestamp(self._clock())]
            if result is not None:
                sets.append("result = ?")
                params.append(json.dumps(result))
            with _transaction(db):
                return db.execute(
                    f"UPDATE approvals SET {', '.join(sets)} WHERE id = ? AND status = ?",
                    (*params, approval_id, expected.value),
                ).rowcount == 1

        return await self._call(write)


class AuditMixin:
    async def add_audit(self, entry: AuditEntry) -> None:
        def write(db: sqlite3.Connection) -> None:
            _insert(db, "audit_log", _row_values(entry))

        await self._call(write)

    async def list_audit(
        self, run_id: str | None = None, limit: int = 200
    ) -> list[AuditEntry]:
        def read(db: sqlite3.Connection) -> list[AuditEntry]:
            where, params = ("WHERE run_id = ?", [run_id]) if run_id else ("", [])
            rows = db.execute(
                f"SELECT * FROM audit_log {where} ORDER BY at DESC, rowid DESC LIMIT ?",
                (*params, limit),
            )
            return [_from_row(AuditEntry, row) for row in rows]

        return await self._call(read)
