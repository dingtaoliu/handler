"""The tenant registry in `handler_control`: who exists and when each must wake.

`tenants` is the source of truth for hosted tenants. `tenant_state` holds the
lifecycle: `active_since` is set while a runtime exists (or a process died holding
one) and `next_wake_at` is when an inactive tenant needs to run.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any

from psycopg import AsyncConnection, sql
from psycopg.types.json import Jsonb

from ..gateway.secret_box import SecretBox
from ..store.postgres import earliest_work_at
from ..store.postgres_hub import CONTROL_SCHEMA, PostgresHub

REGISTRY_LOCK = 7_283_002

_DDL = f"""
CREATE TABLE IF NOT EXISTS {CONTROL_SCHEMA}.tenants (
    name TEXT PRIMARY KEY,
    merchant_id BIGINT UNIQUE,
    schema_name TEXT NOT NULL UNIQUE,
    s3_prefix TEXT NOT NULL DEFAULT '',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    plan TEXT NOT NULL DEFAULT 'paid',
    limits JSONB NOT NULL DEFAULT '{{}}',
    integrations JSONB,
    config JSONB NOT NULL DEFAULT '{{}}',
    secrets TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS {CONTROL_SCHEMA}.tenant_state (
    name TEXT PRIMARY KEY REFERENCES {CONTROL_SCHEMA}.tenants(name),
    active_since TIMESTAMPTZ,
    next_wake_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS tenant_state_next_wake
    ON {CONTROL_SCHEMA}.tenant_state(next_wake_at);
CREATE TABLE IF NOT EXISTS {CONTROL_SCHEMA}.spooled_email (
    tenant TEXT NOT NULL REFERENCES {CONTROL_SCHEMA}.tenants(name),
    digest TEXT NOT NULL,
    body BYTEA NOT NULL,
    headers JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant, digest)
);
CREATE TABLE IF NOT EXISTS {CONTROL_SCHEMA}.registry_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_C = CONTROL_SCHEMA
_SELECT = (f"SELECT t.*, s.active_since, s.next_wake_at FROM {_C}.tenants t "
           f"JOIN {_C}.tenant_state s USING (name)")


@dataclass(frozen=True)
class Tenant:
    name: str
    schema_name: str
    s3_prefix: str
    secrets: str  # sealed: decrypt only to build this tenant's own environment
    merchant_id: int | None = None
    enabled: bool = True
    plan: str = "paid"
    limits: dict[str, Any] = field(default_factory=dict)
    integrations: list[str] | None = None
    config: dict[str, str] = field(default_factory=dict)
    active_since: datetime | None = None
    next_wake_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def describe(self) -> dict[str, Any]:
        """The tenant without its secrets, for the platform API."""
        return {"name": self.name, "merchant_id": self.merchant_id, "schema": self.schema_name,
                "s3_prefix": self.s3_prefix, "enabled": self.enabled, "plan": self.plan,
                "limits": self.limits, "integrations": self.integrations, "config": self.config,
                "base_path": f"/i/{self.name}", "active_since": self.active_since,
                "next_wake_at": self.next_wake_at, "created_at": self.created_at,
                "updated_at": self.updated_at}


def _tenant(row: dict[str, Any]) -> Tenant:
    return Tenant(**{key: row[key] for key in Tenant.__dataclass_fields__})


class TenantRegistry:
    def __init__(self, hub: PostgresHub, box: SecretBox,
                 validate: Callable[[list[Tenant]], None] = lambda tenants: None) -> None:
        self.hub = hub
        self.box = box
        self.validate = validate  # raises ValueError; every check instance_configs makes

    def seal(self, secrets: dict[str, str]) -> str:
        return self.box.encrypt(json.dumps(secrets))

    def unseal(self, tenant: Tenant) -> dict[str, str]:
        return json.loads(self.box.decrypt(tenant.secrets))

    async def setup(self) -> None:
        await self.hub.start()
        async with self.hub.pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (REGISTRY_LOCK,))
            await conn.execute(_DDL)

    async def _all(self, conn: AsyncConnection) -> list[Tenant]:
        cursor = await conn.execute(f"{_SELECT} ORDER BY t.created_at, t.name")
        return [_tenant(row) for row in await cursor.fetchall()]

    async def list(self) -> list[Tenant]:
        async with self.hub.pool.connection() as conn:
            return await self._all(conn)

    async def get(self, name: str) -> Tenant | None:
        async with self.hub.pool.connection() as conn:
            cursor = await conn.execute(f"{_SELECT} WHERE t.name = %s", (name,))
            row = await cursor.fetchone()
        return _tenant(row) if row else None

    async def get_by_merchant(self, merchant_id: int) -> Tenant | None:
        async with self.hub.pool.connection() as conn:
            cursor = await conn.execute(f"{_SELECT} WHERE t.merchant_id = %s", (merchant_id,))
            row = await cursor.fetchone()
        return _tenant(row) if row else None

    # --- writes; serialized so the checks see every other tenant ------------
    async def _insert(self, conn: AsyncConnection, tenant: Tenant) -> None:
        await conn.execute(
            f"INSERT INTO {_C}.tenants (name, merchant_id, schema_name, s3_prefix, enabled, "
            "plan, limits, integrations, config, secrets) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (tenant.name, tenant.merchant_id, tenant.schema_name, tenant.s3_prefix, tenant.enabled,
             tenant.plan, Jsonb(tenant.limits),
             Jsonb(tenant.integrations) if tenant.integrations is not None else None,
             Jsonb(tenant.config), tenant.secrets))
        await conn.execute(f"INSERT INTO {_C}.tenant_state (name) VALUES (%s)", (tenant.name,))

    async def import_once(self, tenants: Sequence[Tenant]) -> bool:
        """Copy the env-defined instances in on the first start with the registry on."""
        if not tenants:
            return False
        async with self.hub.pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (REGISTRY_LOCK,))
            cursor = await conn.execute(
                f"SELECT 1 FROM {_C}.registry_meta WHERE key = 'env_imported'")
            if await cursor.fetchone() is not None:
                return False
            self.validate([*await self._all(conn), *tenants])
            for tenant in tenants:
                await self._insert(conn, tenant)
            await conn.execute(
                f"INSERT INTO {_C}.registry_meta (key, value) VALUES ('env_imported', %s)",
                (",".join(tenant.name for tenant in tenants),))
        return True

    async def upsert_merchant(self, merchant_id: int, changes: dict[str, Any],
                              secrets: dict[str, str | None],
                              make_admin_key: Callable[[], str]) -> tuple[Tenant, bool, str | None]:
        """Create or change a merchant's tenant. Returns it, whether new, any new admin key."""
        async with self.hub.pool.connection() as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(%s)", (REGISTRY_LOCK,))
            tenants = await self._all(conn)
            existing = next((t for t in tenants if t.merchant_id == merchant_id), None)
            if existing is None:
                name = f"m{merchant_id}"
                sealed = {k: v for k, v in secrets.items() if v is not None}
                generated = None
                if not sealed.get("HANDLER_ADMIN_KEY"):
                    generated = sealed["HANDLER_ADMIN_KEY"] = make_admin_key()
                tenant = Tenant(name=name, merchant_id=merchant_id, schema_name=f"handler_{name}",
                                s3_prefix=f"handler/{name}/", secrets=self.seal(sealed), **changes)
                self.validate([*tenants, tenant])
                await self._insert(conn, tenant)
            else:
                merged = {**self.unseal(existing), **secrets}
                merged = {k: v for k, v in merged.items() if v is not None}
                config = {**existing.config, **changes.get("config", {})}
                updated = replace(existing, secrets=self.seal(merged),
                                  **{**changes, "config": {k: v for k, v in config.items()
                                                           if v is not None}})
                self.validate([updated if t.name == updated.name else t for t in tenants])
                await conn.execute(
                    f"UPDATE {_C}.tenants SET plan=%s, limits=%s, integrations=%s, config=%s, "
                    "secrets=%s, updated_at=now() WHERE name=%s",
                    (updated.plan, Jsonb(updated.limits),
                     Jsonb(updated.integrations) if updated.integrations is not None else None,
                     Jsonb(updated.config), updated.secrets, updated.name))
                tenant, generated = updated, None
        return await self.get(tenant.name) or tenant, existing is None, generated

    async def credential_exists(self, schema: str, table: str, column: str, value: str) -> bool:
        """Whether a hashed credential is on file in this tenant's own table."""
        async with self.hub.pool.connection() as conn:
            await self._tenant_transaction(conn, schema)
            cursor = await conn.execute(
                sql.SQL("SELECT 1 FROM {} WHERE {} = %s LIMIT 1").format(
                    sql.Identifier(table), sql.Identifier(column)), (value,))
            return await cursor.fetchone() is not None

    async def has_polling_mailbox(self, schema: str) -> bool:
        """Whether the tenant has an enabled connected mailbox, which its poller polls."""
        async with self.hub.pool.connection() as conn:
            await self._tenant_transaction(conn, schema)
            cursor = await conn.execute(
                "SELECT EXISTS (SELECT 1 FROM mail_accounts WHERE "
                "COALESCE((config::jsonb ->> 'enabled')::boolean, TRUE)) AS polls")
            return (await cursor.fetchone())["polls"]

    async def set_enabled(self, name: str, enabled: bool) -> None:
        async with self.hub.pool.connection() as conn:
            await conn.execute(f"UPDATE {_C}.tenants SET enabled=%s, updated_at=now() WHERE name=%s",
                               (enabled, name))
            if enabled:  # anything that queued while it was off runs now
                await conn.execute(f"UPDATE {_C}.tenant_state SET next_wake_at=now() WHERE name=%s",
                                   (name,))

    async def request_wake(self, name: str) -> None:
        async with self.hub.pool.connection() as conn:
            await conn.execute(
                f"UPDATE {_C}.tenant_state SET next_wake_at = LEAST(next_wake_at, now()) WHERE name=%s",
                (name,))

    # --- email the tenant could not take ------------------------------------
    async def spool_email(self, name: str, digest: str, body: bytes, headers: dict[str, str]) -> None:
        """Keep verified mail, idempotently, and make sure the tenant wakes for it."""
        async with self.hub.pool.connection() as conn:
            await conn.execute(
                f"INSERT INTO {_C}.spooled_email (tenant, digest, body, headers) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (tenant, digest) DO NOTHING",
                (name, digest, body, Jsonb(headers)))
            await conn.execute(
                f"UPDATE {_C}.tenant_state SET next_wake_at = LEAST(next_wake_at, now()) WHERE name=%s",
                (name,))

    async def tenants_with_spooled_email(self) -> list[str]:
        async with self.hub.pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT DISTINCT e.tenant FROM {_C}.spooled_email e "
                f"JOIN {_C}.tenants t ON t.name = e.tenant "
                "WHERE t.enabled AND e.next_attempt_at <= now()")
            return [row["tenant"] for row in await cursor.fetchall()]

    async def spooled_digests(self, name: str) -> list[str]:
        async with self.hub.pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT digest FROM {_C}.spooled_email WHERE tenant=%s AND next_attempt_at <= now() "
                "ORDER BY received_at, digest", (name,))
            return [row["digest"] for row in await cursor.fetchall()]

    async def spooled_message(self, name: str, digest: str) -> tuple[bytes, dict[str, str]] | None:
        async with self.hub.pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT body, headers FROM {_C}.spooled_email WHERE tenant=%s AND digest=%s",
                (name, digest))
            row = await cursor.fetchone()
        return (bytes(row["body"]), row["headers"]) if row else None

    async def spool_delivered(self, name: str, digest: str) -> None:
        async with self.hub.pool.connection() as conn:
            await conn.execute(f"DELETE FROM {_C}.spooled_email WHERE tenant=%s AND digest=%s",
                               (name, digest))

    async def spool_failed(self, name: str, digest: str, error: str,
                           base_seconds: float, cap_seconds: float, max_attempts: int) -> int:
        """Back off, then stop retrying. Never deleted; returns the attempts so far."""
        async with self.hub.pool.connection() as conn:
            cursor = await conn.execute(
                f"UPDATE {_C}.spooled_email SET attempts = attempts + 1, last_error = %s, "
                "next_attempt_at = CASE WHEN attempts + 1 >= %s THEN 'infinity'::timestamptz "
                "ELSE now() + make_interval(secs => LEAST(%s * power(2, attempts), %s)) END "
                "WHERE tenant=%s AND digest=%s RETURNING attempts",
                (error[:500], max_attempts, base_seconds, cap_seconds, name, digest))
            row = await cursor.fetchone()
        return row["attempts"] if row else 0

    # --- lifecycle ----------------------------------------------------------
    async def due(self) -> list[tuple[str, datetime]]:
        """Tenants whose wake time has come; the scan's only query, on control tables."""
        async with self.hub.pool.connection() as conn:
            cursor = await conn.execute(
                f"SELECT s.name, s.next_wake_at FROM {_C}.tenant_state s "
                f"JOIN {_C}.tenants t USING (name) "
                "WHERE t.enabled AND s.next_wake_at <= now()")
            return [(row["name"], row["next_wake_at"]) for row in await cursor.fetchall()]

    async def mark_active(self, name: str) -> None:
        async with self.hub.pool.connection() as conn:
            await conn.execute(
                f"UPDATE {_C}.tenant_state SET active_since=now(), next_wake_at=NULL WHERE name=%s",
                (name,))

    async def activation_failed(self, name: str, retry_seconds: float) -> None:
        """Leave a durable retry: pending work must not depend on the next request."""
        async with self.hub.pool.connection() as conn:
            await conn.execute(
                f"UPDATE {_C}.tenant_state SET active_since=NULL, "
                "next_wake_at=now() + make_interval(secs => %s) WHERE name=%s",
                (retry_seconds, name))

    async def clear_wake(self, name: str, seen: datetime) -> None:
        """Drop a wake request an already-active tenant has now handled."""
        async with self.hub.pool.connection() as conn:
            await conn.execute(
                f"UPDATE {_C}.tenant_state SET next_wake_at=NULL WHERE name=%s AND next_wake_at=%s",
                (name, seen))

    async def _tenant_transaction(self, conn: AsyncConnection, schema: str) -> None:
        await conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))

    async def write_next_wake(self, name: str, schema: str, sinks: Sequence[str] | None = None,
                              *, schedules: bool = True) -> datetime | None:
        """Record when this stopped tenant must run again.

        The row lock waits for any writer's open transaction (a CLI store marks this
        row when it queues work), so their rows are visible to the query below and
        any later writer lands after this and keeps the earlier time.
        """
        async with self.hub.pool.connection() as conn:
            await self._tenant_transaction(conn, schema)
            await conn.execute(f"SELECT 1 FROM {_C}.tenant_state WHERE name=%s FOR UPDATE", (name,))
            due = await earliest_work_at(conn, sinks, schedules=schedules)
            cursor = await conn.execute(
                f"UPDATE {_C}.tenant_state SET next_wake_at=LEAST(next_wake_at, %s) "
                "WHERE name=%s RETURNING next_wake_at", (due, name))
            return (await cursor.fetchone())["next_wake_at"]

    async def clear_active(self, name: str) -> None:
        async with self.hub.pool.connection() as conn:
            await conn.execute(f"UPDATE {_C}.tenant_state SET active_since=NULL WHERE name=%s", (name,))

    async def sweep_tenant(self, name: str, schema: str, *, schedules: bool = True) -> bool:
        """Boot-time check of one tenant's own tables; True if it has work due now."""
        async with self.hub.pool.connection() as conn:
            await self._tenant_transaction(conn, schema)
            due = await earliest_work_at(conn, None, schedules=schedules)
            cursor = await conn.execute(
                f"UPDATE {_C}.tenant_state SET next_wake_at=LEAST(next_wake_at, %s) "
                "WHERE name=%s RETURNING next_wake_at <= now() AS now_due", (due, name))
            return bool((await cursor.fetchone())["now_due"])
