"""The users mixin on SQLite and Postgres."""

from __future__ import annotations

import hashlib
import sqlite3

import psycopg
import pytest

from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.store.sqlite_users import User


async def test_user_crud(users_store, clock):
    store = users_store
    assert await store.list_users() == []
    alice = User("alice", "Alice A", "member", clock())
    assert await store.create_user(alice) is True
    assert await store.create_user(User("alice", "Other")) is False
    clock.advance(1)
    assert await store.create_user(User("root", "Root", "admin", clock()))
    assert await store.get_user("alice") == alice
    assert await store.get_user("nobody") is None
    assert [u.id for u in await store.list_users()] == ["alice", "root"]

    changed = await store.update_user("alice", display_name="Alice B", role="admin", disabled=True)
    assert (changed.display_name, changed.role, changed.disabled) == ("Alice B", "admin", True)
    assert (await store.update_user("alice", disabled=False)).disabled is False
    assert (await store.update_user("alice")).display_name == "Alice B"
    assert await store.update_user("nobody", display_name="x") is None
    with pytest.raises(ValueError):
        await store.update_user("alice", role="superuser")


@pytest.mark.parametrize("user_id", ["", "a:b", "has space", "alice\n", "-lead", "x" * 65])
async def test_invalid_user_ids_and_roles(users_store, user_id):
    with pytest.raises(ValueError):
        await users_store.create_user(User(user_id, "x"))
    with pytest.raises(ValueError):
        await users_store.create_user(User("ok", "x", "owner"))


async def test_token_resolve_revoke_and_last_used(users_store, clock):
    store = users_store
    await store.create_user(User("alice", "Alice", created_at=clock()))
    token_id, token = await store.create_token("alice", "laptop")
    other_id, other = await store.create_token("alice", "phone")
    assert token != other and len(token) >= 43  # token_urlsafe(32)

    (first, second) = await store.list_tokens("alice")
    assert (first.label, first.last_used_at, first.revoked_at) == ("laptop", None, None)

    clock.advance(30)
    user = await store.resolve_token(token)
    assert user.id == "alice"
    assert (await store.list_tokens("alice"))[0].last_used_at == clock()
    assert (await store.list_tokens("alice"))[1].last_used_at is None

    assert await store.resolve_token("not-a-token") is None
    assert await store.resolve_token("") is None
    assert await store.revoke_token(token_id) is True
    assert await store.revoke_token(token_id) is False
    assert await store.revoke_token("missing") is False
    assert await store.resolve_token(token) is None
    assert (await store.resolve_token(other)).id == "alice"

    await store.update_user("alice", disabled=True)
    assert await store.resolve_token(other) is None
    await store.update_user("alice", disabled=False)
    assert (await store.resolve_token(other)).id == "alice"


async def test_token_for_unknown_user_fails(users_store):
    with pytest.raises(Exception):
        await users_store.create_token("ghost")


async def test_sqlite_stores_only_the_hash(tmp_path):
    path = tmp_path / "h.db"
    store = SQLiteStore(path)
    await store.create_user(User("alice", "Alice"))
    _, token = await store.create_token("alice", "label")
    await store.close()
    db = sqlite3.connect(path)
    rows = db.execute("SELECT * FROM user_tokens").fetchall()
    assert len(rows) == 1
    assert hashlib.sha256(token.encode()).hexdigest() in rows[0]
    assert token not in " ".join(str(v) for v in rows[0])


def test_postgres_stores_only_the_hash(pg_dsn):
    import asyncio

    from handler.runtime.store.postgres import PostgresStore

    async def populate() -> str:
        store = PostgresStore(pg_dsn)
        try:
            await store.create_user(User("alice", "Alice"))
            return (await store.create_token("alice", "label"))[1]
        finally:
            await store.close()

    token = asyncio.run(populate())
    with psycopg.connect(pg_dsn) as conn:
        rows = conn.execute("SELECT token_hash FROM user_tokens").fetchall()
    assert rows == [(hashlib.sha256(token.encode()).hexdigest(),)]
