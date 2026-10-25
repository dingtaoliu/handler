"""Authentication: principals, the FastAPI dependencies and the user/token CLI.

Two kinds of bearer credential are accepted:

- the instance admin key (``HANDLER_ADMIN_KEY`` / ``--admin-key``), compared in
  constant time. A request carrying it may act for a user with
  ``X-Handler-User: <user_id>``; it then has that user's role and visibility;
- a user token, which resolves to its (enabled) user.

Tokens are never logged or echoed. Only SSE routes also accept
``?access_token=`` (``EventSource`` cannot send headers).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException, Request

from .store import sqlite as _sqlite  # noqa: F401  (sqlite must load first)
from .store.sqlite_users import USER_ID, User, UsersStore

Via = Literal["admin_key", "user_token", "trusted_network"]
ACT_AS_HEADER = "X-Handler-User"
_BEARER = {"WWW-Authenticate": "Bearer"}


@dataclass(frozen=True)
class Principal:
    user_id: str | None
    role: str
    via: Via

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def identity(self) -> str:
        """Who to record as the actor in audit trails."""
        return self.user_id or "admin"


def can_see_thread(principal: Principal, thread_key: str) -> bool:
    """Members see their own ``chat:`` threads and every shared thread."""
    if principal.is_admin or not thread_key.startswith("chat:"):
        return True
    return principal.user_id is not None and thread_key.startswith(
        f"chat:{principal.user_id}:"
    )


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


class Authenticator:
    """Resolves requests to principals; its methods are FastAPI dependencies."""

    def __init__(self, users: UsersStore, admin_key: str | None = None,
                 trusted_user_id: str | None = None,
                 trusted_device_users: Mapping[str, str] | None = None) -> None:
        self.users = users
        self._admin_digest = _digest(admin_key) if admin_key else None
        self._trusted_user_id = trusted_user_id.strip() if trusted_user_id else None
        self._trusted_device_users = dict(trusted_device_users or {})

    def _is_admin_key(self, credential: str) -> bool:
        if self._admin_digest is None:
            return False
        return hmac.compare_digest(_digest(credential), self._admin_digest)

    async def authenticate(self, credential: str, act_as: str | None) -> Principal | None:
        if self._is_admin_key(credential):
            if not act_as:
                return Principal(None, "admin", "admin_key")
            user = await self.users.get_user(act_as)
            if user is None or user.disabled:
                raise HTTPException(403, "Unknown or disabled user in " + ACT_AS_HEADER)
            return Principal(user.id, user.role, "admin_key")
        user = await self.users.resolve_token(credential)
        return Principal(user.id, user.role, "user_token") if user else None

    async def _principal(self, request: Request, allow_query: bool) -> Principal:
        authorization = request.headers.get("authorization")
        credential = _bearer(authorization)
        if credential is None and allow_query:
            credential = request.query_params.get("access_token") or None
        if credential is None:
            # A malformed/presented credential must never fall through to trusted mode.
            if authorization is not None:
                raise HTTPException(401, "Invalid credentials", headers=_BEARER)
            peer = request.client.host if request.client else ""
            trusted_user_id = self._trusted_device_users.get(peer) or self._trusted_user_id
            if trusted_user_id:
                user = await self.users.get_user(trusted_user_id)
                if user is None or user.disabled:
                    raise HTTPException(503, "Trusted user is unknown or disabled")
                return Principal(user.id, user.role, "trusted_network")
            raise HTTPException(401, "Authentication required", headers=_BEARER)
        act_as = request.headers.get(ACT_AS_HEADER)
        if act_as is None and allow_query:
            act_as = request.query_params.get("act_as")
        principal = await self.authenticate(credential, act_as)
        if principal is None:
            raise HTTPException(401, "Invalid credentials", headers=_BEARER)
        return principal

    async def require_principal(self, request: Request) -> Principal:
        return await self._principal(request, allow_query=False)

    async def require_sse_principal(self, request: Request) -> Principal:
        """Like require_principal, plus ``?access_token=``. SSE routes only."""
        return await self._principal(request, allow_query=True)

    async def require_admin(self, request: Request) -> Principal:
        principal = await self._principal(request, allow_query=False)
        if not principal.is_admin:
            raise HTTPException(403, "Admin role required")
        return principal


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, credential = header.partition(" ")
    credential = credential.strip()
    return credential if scheme.lower() == "bearer" and credential else None


# --- CLI: handler-rt user add / token create ---------------------------------
def add_cli_commands(subparsers: argparse._SubParsersAction) -> None:
    def storage(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--data-dir",
                            default=os.environ.get("HANDLER_RT_DATA_DIR", "~/.handler-rt"))
        parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
        parser.add_argument("--db-schema", default=os.environ.get("HANDLER_DB_SCHEMA"))

    user = subparsers.add_parser("user", help="manage users")
    actions = user.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add", help="create a user")
    add.add_argument("user_id")
    add.add_argument("--admin", action="store_true", help="give the admin role")
    add.add_argument("--display-name", default=None)
    storage(add)

    token = subparsers.add_parser("token", help="manage API tokens")
    actions = token.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create", help="create a token (printed once)")
    create.add_argument("user_id")
    create.add_argument("--label", default="")
    storage(create)

    thread = subparsers.add_parser("thread", help="repair threads")
    actions = thread.add_subparsers(dest="action", required=True)
    revive = actions.add_parser(
        "revive", help="make a dead thread claimable again (clears dead and attempts)")
    revive.add_argument("thread_key")
    revive.add_argument("--reset-session", action="store_true",
                        help="drop the harness session: the next turn starts a fresh one "
                             "from a recap of the thread's transcript")
    revive.add_argument("--discard-pending", action="store_true",
                        help="drop the unanswered events instead of running them again")
    storage(revive)


def open_store(args: argparse.Namespace):
    from pathlib import Path

    from .store.postgres import PostgresStore
    from .store.sqlite import SQLiteStore

    if args.database_url:
        kwargs = {"schema": args.db_schema} if args.db_schema else {}
        return PostgresStore(args.database_url, **kwargs)
    data_dir = Path(args.data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    return SQLiteStore(data_dir / "handler.db")


async def _run_cli(args: argparse.Namespace, store=None) -> int:
    store = store or open_store(args)
    try:
        if args.command == "thread":
            return await _revive_thread(store, args)
        if args.command == "user":
            if not USER_ID.fullmatch(args.user_id):
                print(f"error: invalid user id (use {USER_ID.pattern})", file=sys.stderr)
                return 2
            user = User(args.user_id, args.display_name or args.user_id,
                        "admin" if args.admin else "member")
            if not await store.create_user(user):
                print(f"error: user {user.id} already exists", file=sys.stderr)
                return 1
            print(f"created {user.role} {user.id}")
            return 0
        if await store.get_user(args.user_id) is None:
            print(f"error: unknown user {args.user_id}", file=sys.stderr)
            return 1
        token_id, token = await store.create_token(args.user_id, args.label)
        print(f"token_id: {token_id}", file=sys.stderr)
        print(token)
        print("Store this token now; it cannot be shown again.", file=sys.stderr)
        return 0
    finally:
        await store.close()


async def _revive_thread(store, args: argparse.Namespace) -> int:
    before = await store.get_thread(args.thread_key)
    if before is None:
        print(f"error: unknown thread {args.thread_key}", file=sys.stderr)
        return 1
    swept = await store.sweep_abandoned_runs(args.thread_key)
    await store.revive_thread(args.thread_key, reset_session=args.reset_session,
                              discard_pending=args.discard_pending)
    after = await store.get_thread(args.thread_key)
    pending = len(await store.pending_events(args.thread_key))
    print(f"revived {args.thread_key}: dead {before.dead} -> {after.dead}, "
          f"attempts {before.attempts} -> {after.attempts}, "
          f"session {before.session_id or '-'} -> {after.session_id or '-'}, "
          f"pending events {pending}, runs marked abandoned {len(swept)}")
    return 0


def run_cli(args: argparse.Namespace) -> int:
    return asyncio.run(_run_cli(args))
