"""Production's shape for rehearsals: three env-defined instances on one database.

Mirrors `crontal-agent` (docs/rebuild/HANDOFF.md §5, multi-instance.md): `main` keeps
the unprefixed variables with schema `handler_crontal` and prefix `handler/crontal/`;
`bss` and `yeup` take `HANDLER_I_<NAME>__*` overrides and the default schemas
`handler_bss` and `handler_yeup`. Every value here is a test value.
"""
from __future__ import annotations

import asyncio
import contextlib
import socket

import httpx
import uvicorn

from handler.runtime import server
from handler.runtime.harness.base import HarnessError, RunEvent, RunEventKind
from handler.runtime.types import TokenUsage

CRONTAL_URL = "http://crontal.invalid/api/v1/agent-mcp"
MERCHANTS = {"main": 2, "bss": 1, "yeup": 298}  # runbook stage 1
SCHEMAS = {"main": "handler_crontal", "bss": "handler_bss", "yeup": "handler_yeup"}
PREFIXES = {"main": "handler/crontal/", "bss": "handler/bss/", "yeup": "handler/yeup/"}
PLATFORM_KEY = "rehearsal-platform-key"
SEALING_KEY = "rehearsal-sealing-key"


def admin_key(name: str) -> str:
    return f"{name}-admin-key"


def webhook_secret(name: str) -> str:
    return f"{name}-webhook-secret"


def production_env(tmp_path, dsn: str, port: int = 8100) -> dict[str, str]:
    env = {
        "DATABASE_URL": dsn,
        "PORT": str(port),
        "HANDLER_RT_DATA_DIR": str(tmp_path / "data"),
        "HANDLER_INSTANCES": "main,bss,yeup",
        "HANDLER_DB_SCHEMA": SCHEMAS["main"],
        "HANDLER_FILE_PREFIX": PREFIXES["main"],
        "HANDLER_ALLOWED_HARNESSES": "openai_agents",
        "HANDLER_DEFAULT_HARNESS": "openai_agents",
        "OPENAI_API_KEY": "fake-no-network",
        # main: unprefixed, as today
        "HANDLER_ADMIN_KEY": admin_key("main"),
        "HANDLER_WEBHOOK_SECRET": webhook_secret("main"),
        "HANDLER_SECRET_KEY": "main-secret-key",
        "HANDLER_PUBLIC_URL": "https://handler.example",
        "CRONTAL_URL": CRONTAL_URL,
        "CRONTAL_AGENT_TOKEN": "main-agent-token",
        "GOOGLE_OAUTH_CLIENT_ID": "main-google-client",
        "GOOGLE_OAUTH_CLIENT_SECRET": "main-google-secret",
        "HANDLER_I_MAIN__HANDLER_MERCHANT_ID": str(MERCHANTS["main"]),
    }
    for name in ("bss", "yeup"):
        prefix = f"HANDLER_I_{name.upper()}__"
        env.update({
            prefix + "HANDLER_ADMIN_KEY": admin_key(name),
            prefix + "HANDLER_WEBHOOK_SECRET": webhook_secret(name),
            prefix + "HANDLER_SECRET_KEY": f"{name}-secret-key",
            prefix + "HANDLER_PUBLIC_URL": f"https://handler.example/i/{name}",
            prefix + "CRONTAL_URL": CRONTAL_URL,
            prefix + "CRONTAL_AGENT_TOKEN": f"{name}-agent-token",
            prefix + "HANDLER_MERCHANT_ID": str(MERCHANTS[name]),
        })
    # BSS reads a company mailbox (IMAP/SMTP); its email intake has its own secret.
    env["HANDLER_I_BSS__INTEGRATIONS"] = "crontal,mailbox"
    env["HANDLER_I_BSS__HANDLER_EMAIL_SECRET"] = "bss-email-secret"
    env["HANDLER_I_BSS__HANDLER_MAX_CONCURRENT"] = "5"
    return env


def registry_on(env: dict[str, str]) -> dict[str, str]:
    return {**env, "HANDLER_TENANT_REGISTRY": "1", "HANDLER_PLATFORM_KEY": PLATFORM_KEY,
            "HANDLER_TENANT_SECRET_KEY": SEALING_KEY}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeHarness:
    """The native harness's stand-in: answers every turn, lists its tools over the
    MCP URL and run token it was given (so the host's MCP route is exercised), and
    fails a turn that says FAIL until `failing` is cleared."""

    name = "openai_agents"
    runs: list[dict] = []
    failing = True
    gate: asyncio.Event | None = None

    def __init__(self, *args, **kwargs):
        pass

    async def run(self, req):
        tools = await _list_tools(req.mcp_servers)
        FakeHarness.runs.append({"prompt": req.prompt, "model": req.model, "tools": tools,
                                 "mcp": [s.url for s in req.mcp_servers]})
        if "FAIL" in req.prompt and FakeHarness.failing:
            raise HarnessError("provider down", retryable=False)
        yield RunEvent(RunEventKind.SESSION, {"session_id": "s"})
        if FakeHarness.gate is not None:
            await FakeHarness.gate.wait()
        yield RunEvent(RunEventKind.USAGE, {"model": req.model or "fake"},
                       usage=TokenUsage(input_tokens=1))
        text = f"answered: {req.prompt[-40:]}"
        yield RunEvent(RunEventKind.ASSISTANT_TEXT, {"text": text})
        yield RunEvent(RunEventKind.FINAL, {"text": text, "session_id": "s"})

    async def interrupt(self, run_id):
        pass


async def _list_tools(servers) -> list[str] | None:
    """tools/list over the given MCP server, as the Agents SDK would."""
    if not servers or not servers[0].url:
        return None
    spec = servers[0]
    headers = {**spec.headers, "Accept": "application/json, text/event-stream"}
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(spec.url, json=body, headers=headers)
    except httpx.HTTPError:
        return None  # no live server (an ASGI-only test)
    if response.status_code != 200:
        return [f"HTTP {response.status_code}"]
    return sorted(tool["name"] for tool in response.json()["result"]["tools"])


def patch_runtime(monkeypatch, discovery=None) -> None:
    def no_codex():
        raise FileNotFoundError("test has no codex")

    monkeypatch.setattr(server, "codex_binary", no_codex)
    monkeypatch.setattr(server, "OpenAIAgentsHarness", FakeHarness)
    FakeHarness.runs, FakeHarness.failing, FakeHarness.gate = [], True, None


@contextlib.asynccontextmanager
async def live(env: dict[str, str]):
    """The host on a real loopback port, as production serves it."""
    app = server.create_host(server._parse_args(["serve"], env), env)
    port = int(env["PORT"])
    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None,
                                       access_log=False, lifespan="on"))
    task = asyncio.create_task(uv.serve())
    try:
        for _ in range(500):
            if uv.started or task.done():
                break
            await asyncio.sleep(0.02)
        if task.done():
            task.result()
        assert uv.started, "server did not start"
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
            yield app, client
    finally:
        uv.should_exit = True
        await asyncio.wait_for(task, 30)
