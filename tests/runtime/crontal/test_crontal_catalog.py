"""Crontal's API catalog in the stable part of the instructions."""

from __future__ import annotations

import asyncio
import json
import random
from pathlib import Path

import httpx
import pytest

from handler.runtime.auth import Authenticator
from handler.runtime.bus import RunEventBus
from handler.runtime.gateway import crontal_catalog as catalog_module
from handler.runtime.gateway.crontal_api import CrontalApiTools
from handler.runtime.gateway.crontal_catalog import (
    DEFAULT_FULL_SPECS,
    DEFAULT_RESOURCES,
    CrontalRouteCatalog,
    crontal_route_catalog,
    render_catalog,
    spec_key,
)
from handler.runtime.instructions import (
    CATALOG_UNAVAILABLE_HINT,
    CRONTAL_CATALOG_GUIDANCE,
    CRONTAL_DISCOVERY_GUIDANCE,
    build_instruction_components,
)
from handler.runtime.memory import MemoryTools
from handler.runtime.observability.prompt_tokens import PromptComponent, prompt_token_breakdown
from handler.runtime.settings import EfficiencySettings, SettingsService
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.types import ThreadType
from handler.runtime.web.app import create_app
from handler.runtime.web.settings_api import settings_router

FIXTURE = Path(__file__).parents[1] / "fixtures" / "crontal" / "route_catalog.json"
AGENT = "agent-secret"
ADMIN = "admin-key"
TOOLS = {"crontal_api_routes", "crontal_api_describe", "crontal_api_get", "crontal_api_call"}


@pytest.fixture(scope="module")
def fixture() -> dict:
    """Crontal's real route list and default contracts (see the file's `source`)."""
    return json.loads(FIXTURE.read_text())


class FakeCrontal:
    """Serves the fixture's catalog; counts and optionally delays/fails fetches."""

    def __init__(self, data: dict) -> None:
        self.data = data
        self.route_fetches = 0
        self.describes = 0
        self.fail = False
        self.delay = 0.0

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {AGENT}"
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            return httpx.Response(503, json={"error": "down"})
        if request.url.path == "/api/v1/agent-access/routes":
            self.route_fetches += 1
            return httpx.Response(200, json={"routes": self.data["routes"]})
        if request.url.path == "/api/v1/agent-access/routes/describe":
            self.describes += 1
            q = dict(request.url.params)
            spec = self.data["specs"].get(f"{q['method']} {q['path']}")
            if spec is None:
                return httpx.Response(200, json={"method": q["method"], "path": q["path"],
                                                 "summary": "", "has_schema": False})
            return httpx.Response(200, json=spec)
        return httpx.Response(404, json={"error": "not found"})


def make_catalog(fake: FakeCrontal, **kwargs) -> CrontalRouteCatalog:
    client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
    return CrontalRouteCatalog("https://crontal.test", AGENT, client=client, **kwargs)


def spec_map(data: dict) -> dict:
    return {spec_key(k): v for k, v in data["specs"].items()}


# ── rendering ────────────────────────────────────────────────────────────────


def test_render_is_grouped_sorted_and_byte_stable(fixture):
    routes = fixture["routes"]
    text = render_catalog(routes, spec_map(fixture), DEFAULT_FULL_SPECS, 10**6)
    shuffled = list(routes)
    random.Random(4).shuffle(shuffled)
    assert render_catalog(shuffled, spec_map(fixture), DEFAULT_FULL_SPECS, 10**6) == text
    lines = text.split("## Contracts")[0]
    # Every visible route is one line, with readable path parameters.
    assert lines.count("\n[rfq]\n") == 1 and "\n[files]\n" in lines
    # Paths drop /api/v1 (the header says to prepend it); summaries that only
    # restate the method and path are left out.
    assert "Paths omit the /api/v1 prefix: always prepend it" in text
    assert "\nGET /rfq/received/{rfq_id}\n" in lines
    assert "\nPOST /rfq/received/extract — Extract a draft received RFQ from an uploaded\n" in lines
    assert "\nDELETE /files/{file_id} — Move a file or folder to the trash (restorable)\n" in lines
    assert "/api/v1/" not in lines.split("\n\n", 2)[2]
    assert len([ln for ln in lines.splitlines() if ln.startswith(("GET", "POST", "PUT",
                                                                  "PATCH", "DELETE"))]) \
        == len(routes)
    # Contracts follow the configured priority order, as compact sorted JSON.
    contracts = text.split("## Contracts")[1]
    headings = [ln for ln in contracts.splitlines() if ln.startswith("### ")]
    assert len(headings) == len(DEFAULT_FULL_SPECS)
    assert headings[0] == "### GET /rfq/received"
    assert '"has_schema"' not in contracts and "<int:" not in text
    for line in contracts.splitlines():
        if line.startswith("{"):
            assert json.dumps(json.loads(line), ensure_ascii=False, separators=(",", ":"),
                              sort_keys=True) == line


def test_hidden_routes_are_left_out():
    routes = [{"methods": ["GET"], "path": "/api/v1/rfq", "summary": "RFQs"},
              {"methods": ["POST"], "path": "/api/v1/agent-access/token", "summary": "mint"},
              {"methods": ["GET"], "path": "/api/v1/admin/users", "summary": "admin"},
              {"methods": ["HEAD"], "path": "/api/v1/time", "summary": "head only"}]
    text = render_catalog(routes, {}, [], 10_000)
    assert text.endswith("\n[rfq]\nGET /rfq")
    assert "agent-access" not in text and "admin" not in text and "/time" not in text


def test_cap_keeps_every_route_line_and_drops_specs(fixture):
    full = render_catalog(fixture["routes"], spec_map(fixture), DEFAULT_FULL_SPECS, 10**6)
    lines_only = render_catalog(fixture["routes"], spec_map(fixture), [], 10**6)
    small = render_catalog(fixture["routes"], spec_map(fixture), DEFAULT_FULL_SPECS,
                           len(lines_only) + 3_000)
    assert len(small) <= len(lines_only) + 3_000
    assert small.startswith(lines_only)
    assert 0 < small.count("### ") < full.count("### ")
    # Greedy in priority order: the first contract always comes first.
    assert "### GET /rfq/received\n" in small
    # Route lines alone over the cap: no catalog (the prompt falls back).
    assert render_catalog(fixture["routes"], {}, [], len(lines_only) - 1) is None


def tokens(text: str) -> int:
    return prompt_token_breakdown([PromptComponent("catalog", text)])["total_tokens"]


def test_resources_limit_the_route_lines(fixture):
    text = render_catalog(fixture["routes"], spec_map(fixture), [], 10**6, DEFAULT_RESOURCES)
    assert "\nPOST /rfq/received — " in text and "\nGET /operations/deals" in text
    assert "/auth/" not in text and "\nGET /supply_listing" not in text
    assert "[agent]" not in text
    only = render_catalog(fixture["routes"], {}, [], 10**6, ["/api/v1/rfq/received"])
    assert "[rfq]" in only and "\nGET /rfq\n" not in only and "[files]" not in only


def test_default_catalog_stays_small(fixture):
    """The realistic catalog at default settings (sizes in the implementation doc)."""
    defaults = EfficiencySettings()
    cap, resources = defaults.route_catalog_max_chars, defaults.route_catalog_resources
    lines = render_catalog(fixture["routes"], spec_map(fixture), [], cap, resources)
    assert lines is not None and tokens(lines) <= 1_500
    text = render_catalog(fixture["routes"], spec_map(fixture),
                          defaults.route_catalog_full_specs, cap, resources)
    assert len(text) <= cap and tokens(text) <= 1_800
    # The most-used route's contract fits; the rest wait for a larger cap.
    assert [ln for ln in text.splitlines() if ln.startswith("### ")] \
        == ["### GET /rfq/received"]
    everything = render_catalog(fixture["routes"], spec_map(fixture),
                                defaults.route_catalog_full_specs, 15_000, resources)
    assert everything.count("### ") == len(DEFAULT_FULL_SPECS)


def test_settings_validate_full_spec_entries():
    assert spec_key("get /api/v1/rfq") == ("GET", "/api/v1/rfq")
    assert spec_key("FETCH /api/v1/rfq") is None and spec_key("GET rfq") is None
    with pytest.raises(ValueError):
        EfficiencySettings(route_catalog_full_specs=["GET rfq"])
    with pytest.raises(ValueError):
        EfficiencySettings(route_catalog_max_chars=10)
    for bad in ("rfq", "/api/v2/rfq", "/api/v1/rfq/<int:rfq_id>", "/api/v1/r fq"):
        with pytest.raises(ValueError):
            EfficiencySettings(route_catalog_resources=[bad])
    assert EfficiencySettings(route_catalog_resources=[" /api/v1/files/ "]
                              ).route_catalog_resources == ["/api/v1/files"]


# ── caching ──────────────────────────────────────────────────────────────────


async def test_prompt_text_is_cached_and_shared_with_the_tools(fixture, tmp_path):
    fake = FakeCrontal(fixture)
    catalog = make_catalog(fake)
    first = await catalog.prompt_text(max_chars=40_000, full_specs=DEFAULT_FULL_SPECS)
    assert first and fake.route_fetches == 1
    assert fake.describes == len(DEFAULT_FULL_SPECS)
    again = await catalog.prompt_text(max_chars=40_000, full_specs=DEFAULT_FULL_SPECS)
    assert again == first and fake.route_fetches == 1
    assert fake.describes == len(DEFAULT_FULL_SPECS)
    # The crontal_api tools read through the same caches.
    tools = CrontalApiTools("https://crontal.test", AGENT, workspace_for=lambda k: tmp_path,
                            catalog=catalog)
    assert tools.catalog is catalog
    from handler.runtime.context import RunContext
    ctx = RunContext(run_id="r", thread_key="chat:1:c", user_id="1", acting_as="1")
    by_name = {s.name: s for s in tools.tools()}
    listed = await by_name["crontal_api_routes"].handler(ctx, {"search": "rfq"})
    assert listed.content and fake.route_fetches == 1
    described = await by_name["crontal_api_describe"].handler(
        ctx, {"method": "GET", "path": "/api/v1/rfq/received"})
    assert not described.is_error and fake.describes == len(DEFAULT_FULL_SPECS)


async def test_stale_catalog_is_served_while_refreshing(fixture, monkeypatch):
    fake = FakeCrontal(fixture)
    catalog = make_catalog(fake, prompt_ttl=3600)
    first = await catalog.prompt_text(max_chars=40_000, full_specs=["GET /api/v1/rfq"])
    clock = catalog_module.time.time() + 4000
    monkeypatch.setattr(catalog_module.time, "time", lambda: clock)
    fake.delay = 0.2  # a slow refresh doesn't block the run
    started = asyncio.get_running_loop().time()
    stale = await catalog.prompt_text(max_chars=40_000, full_specs=["GET /api/v1/rfq"])
    assert stale == first and asyncio.get_running_loop().time() - started < 0.1
    await asyncio.sleep(0.5)
    assert fake.route_fetches == 2  # refreshed in the background


async def test_slow_first_fetch_falls_back_then_recovers(fixture):
    fake = FakeCrontal(fixture)
    fake.delay = 0.3
    catalog = make_catalog(fake, wait_seconds=0.05)
    assert await catalog.prompt_text(max_chars=40_000, full_specs=[]) is None
    await asyncio.sleep(0.5)  # the fetch kept running for the next run
    assert await catalog.prompt_text(max_chars=40_000, full_specs=[])


async def test_failed_fetch_falls_back_without_waiting_again(fixture, monkeypatch):
    fake = FakeCrontal(fixture)
    fake.fail = True
    catalog = make_catalog(fake)
    assert await catalog.prompt_text(max_chars=40_000, full_specs=[]) is None
    fetches = fake.route_fetches
    fake.fail = False
    # Within the retry window: no new fetch, immediate fallback.
    assert await catalog.prompt_text(max_chars=40_000, full_specs=[]) is None
    assert fake.route_fetches == fetches
    clock = catalog_module.time.time() + catalog_module.FAILURE_RETRY_SECONDS + 1
    monkeypatch.setattr(catalog_module.time, "time", lambda: clock)
    assert await catalog.prompt_text(max_chars=40_000, full_specs=[])


def test_shared_catalog_per_crontal_and_token():
    one = crontal_route_catalog("https://c.test/", "token-a")
    assert crontal_route_catalog("https://c.test", "token-a") is one
    assert crontal_route_catalog("https://c.test", "token-b") is not one


# ── instructions and preview ─────────────────────────────────────────────────


class _Catalog:
    def __init__(self, text):
        self.text, self.calls = text, []

    async def prompt_text(self, *, max_chars, full_specs, resources=None):
        self.calls.append((max_chars, list(full_specs), resources))
        return self.text


async def _components(tmp_path, catalog, tools=TOOLS, thread_type=ThreadType.CHAT, efficiency=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(tmp_path / "h.db")
    try:
        settings = SettingsService(store)
        if efficiency:
            await settings.put("efficiency", efficiency, "t", None)
        return await build_instruction_components(
            settings, MemoryTools(store), speaker="7", thread_type=thread_type,
            available_tools=tools, crontal_catalog=catalog)
    finally:
        await store.close()


async def test_catalog_is_the_last_stable_component(tmp_path):
    catalog = _Catalog("# Crontal API catalog\nGET /api/v1/rfq — RFQs")
    parts = await _components(tmp_path, catalog)
    names = [p.name for p in parts]
    assert names.index("crontal_tool_guidance") < names.index("crontal_route_catalog") \
        < names.index("memory_rules") < names.index("memory_index")
    assert not any(p.dynamic for p in parts[:names.index("crontal_route_catalog") + 1])
    guidance = next(p.text for p in parts if p.name == "crontal_tool_guidance")
    assert guidance == CRONTAL_CATALOG_GUIDANCE
    assert catalog.calls == [(7_000, DEFAULT_FULL_SPECS, DEFAULT_RESOURCES)]


async def test_no_catalog_without_crontal_tools_or_when_disabled(tmp_path):
    catalog = _Catalog("# Crontal API catalog")
    names = [p.name for p in await _components(tmp_path / "a", catalog, tools={"memory_read"})]
    assert "crontal_route_catalog" not in names and "crontal_tool_guidance" not in names
    assert catalog.calls == []
    parts = await _components(tmp_path / "b", catalog, efficiency={"route_catalog": False})
    assert "crontal_route_catalog" not in [p.name for p in parts]
    # Disabled: exactly today's discovery guidance.
    assert next(p.text for p in parts if p.name == "crontal_tool_guidance") \
        == CRONTAL_DISCOVERY_GUIDANCE


async def test_unavailable_catalog_falls_back_with_a_hint(tmp_path):
    parts = await _components(tmp_path / "a", _Catalog(None))
    assert "crontal_route_catalog" not in [p.name for p in parts]
    assert next(p.text for p in parts if p.name == "crontal_tool_guidance") \
        == CRONTAL_DISCOVERY_GUIDANCE + CATALOG_UNAVAILABLE_HINT

    class Broken:
        async def prompt_text(self, **_):
            raise RuntimeError("boom")

    parts = await _components(tmp_path / "b", Broken())
    assert "crontal_route_catalog" not in [p.name for p in parts]


async def test_settings_reach_the_catalog(tmp_path):
    catalog = _Catalog("# Crontal API catalog")
    await _components(tmp_path, catalog, efficiency={
        "route_catalog_max_chars": 5000, "route_catalog_full_specs": ["GET /api/v1/rfq"],
        "route_catalog_resources": ["/api/v1/rfq"]})
    assert catalog.calls == [(5000, ["GET /api/v1/rfq"], ["/api/v1/rfq"])]


async def test_prompt_preview_shows_the_catalog_component(tmp_path, fixture):
    store = SQLiteStore(tmp_path / "h.db")
    try:
        service = SettingsService(store)
        catalog = make_catalog(FakeCrontal(fixture))
        auth = Authenticator(store, ADMIN)
        app = create_app(store, RunEventBus(), auth)
        app.include_router(settings_router(service, auth, crontal_catalog=catalog,
                                           available_tools=lambda: TOOLS))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://t",
                                     headers={"Authorization": f"Bearer {ADMIN}"}) as client:
            body = (await client.get("/api/settings/prompt",
                                     params={"thread_type": "email"})).json()
        rows = {r["name"]: r for r in body["token_breakdown"]["components"]}
        row = rows["crontal_route_catalog"]
        assert not row["dynamic"] and 1_000 < row["tokens"] <= 1_800
        assert "# Crontal API catalog" in body["prompt"]
        assert "### GET /rfq/received" in body["prompt"]
        assert "\nGET /supply_listing" not in body["prompt"]
    finally:
        await store.close()


def test_canonical_route_description_forms_and_specificity():
    from handler.runtime.gateway.crontal_catalog import canonical_route
    routes = [
        {"path": "/api/v1/files/<int:file_id>", "methods": ["GET"]},
        {"path": "/api/v1/files/browse", "methods": ["GET"]},
        {"path": "/api/v1/contact/list/<int:contact_list_id>/contact/<int:contact_id>", "methods": ["POST"]},
        {"path": "/api/v1/contact/<string:name>", "methods": ["GET"]},
    ]
    for path in ("/api/v1/files/{file_id}", "/api/v1/files/<int:file_id>",
                 "/api/v1/files/15?download=1", "/api/v1/files/15/"):
        assert canonical_route("GET", path, routes) == "/api/v1/files/<int:file_id>"
    assert canonical_route("POST", "/api/v1/contact/list/{contact_list_id}/contact/{contact_id}", routes) == routes[2]["path"]
    assert canonical_route("GET", "/api/v1/files/browse", routes) == routes[1]["path"]
    assert canonical_route("GET", "/api/v1/files/abc", routes) == "/api/v1/files/abc"
    assert canonical_route("GET", "/api/v1/contact/Alice", routes) == routes[3]["path"]
    with pytest.raises(ValueError, match="No published"):
        canonical_route("DELETE", "/api/v1/files/{file_id}", routes)
    with pytest.raises(ValueError, match="not available"):
        canonical_route("GET", "/api/v1/admin/users", routes)
    with pytest.raises(ValueError, match="Ambiguous"):
        canonical_route("GET", "/api/v1/contact/Alice", routes + [
            {"path": "/api/v1/contact/<string:other>", "methods": ["GET"]}])


async def test_description_aliases_share_cache(fixture):
    fake = FakeCrontal(fixture)
    catalog = make_catalog(fake)
    path = next(r["path"] for r in fixture["routes"]
                if "<int:" in r["path"] and "GET" in r["methods"]
                and not catalog_module.hidden(r["path"]))
    brace = catalog_module._PARAM.sub(r"{\1}", path)
    concrete = catalog_module._PARAM.sub("17", path)
    try:
        a = await catalog.spec("get", brace)
        assert isinstance(a, dict) and a["path"] == path
        assert await catalog.spec("GET", path) == a
        assert await catalog.spec("GET", concrete + "?view=1") == a
        assert fake.describes == 1
    finally:
        await catalog._client.aclose()
