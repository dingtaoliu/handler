"""A single HTTP host owns isolated runtimes, including their background lifetimes."""
import asyncio
import contextlib
import json
import os

import httpx
import pytest

from handler.runtime import server
from handler.runtime.web.ingest import sign
from handler.runtime.gateway.secret_box import SecretBox
from handler.runtime.harness.codex import CodexHarness


def config(tmp_path):
    return {
        'HANDLER_INSTANCES': 'main,bss',
        'HANDLER_RT_DATA_DIR': str(tmp_path / 'main'),
        'HANDLER_ADMIN_KEY': 'main-admin',
        'HANDLER_WEBHOOK_SECRET': 'main-hook',
        'HANDLER_EMAIL_SECRET': 'main-email',
        'HANDLER_SECRET_KEY': 'main-encryption',
        'GOOGLE_OAUTH_CLIENT_ID': 'main-client',
        'GOOGLE_OAUTH_CLIENT_SECRET': 'main-secret',
        'HANDLER_PUBLIC_URL': 'https://handler.example',
        'HANDLER_I_BSS__HANDLER_ADMIN_KEY': 'bss-admin',
        'HANDLER_I_BSS__HANDLER_WEBHOOK_SECRET': 'bss-hook',
        'OPENAI_API_KEY': 'fake-no-network',
        'HANDLER_DEFAULT_HARNESS': 'openai_agents',
    }


def test_configuration_isolated_without_process_mutation(tmp_path):
    env = config(tmp_path)
    before = dict(os.environ)
    main, bss = server.instance_configs(server._parse_args(['serve'], env), env)
    assert main[2]['HANDLER_PUBLIC_URL'] == 'https://handler.example'
    assert main[1].data_dir == str(tmp_path / 'main')
    assert bss[1].data_dir == str(tmp_path / 'main/instances/bss')
    assert bss[1].db_schema == 'handler_bss'
    assert bss[2]['HANDLER_FILE_PREFIX'] == 'handler/bss/'
    for key in ('HANDLER_EMAIL_SECRET', 'HANDLER_SECRET_KEY', 'HANDLER_PUBLIC_URL',
                'GOOGLE_OAUTH_CLIENT_ID', 'GOOGLE_OAUTH_CLIENT_SECRET'):
        assert key not in bss[2]
    assert not any(k.startswith('HANDLER_I_') for k in bss[2])
    assert dict(os.environ) == before


@pytest.mark.parametrize('key,value', [
    ('HANDLER_INSTANCES', 'main,main'),
    ('HANDLER_INSTANCES', 'main,../bss'),
    ('HANDLER_I_BSS__HANDLER_ADMIN_KEY', 'main-admin'),
    ('HANDLER_I_BSS__HANDLER_RT_DATA_DIR', 'SAME'),
    ('HANDLER_I_BSS__HANDLER_WEBHOOK_SECRET', 'main-hook'),
])
def test_rejects_ambiguous_instance_config(tmp_path, key, value):
    env = config(tmp_path)
    env[key] = env['HANDLER_RT_DATA_DIR'] if value == 'SAME' else value
    with pytest.raises(ValueError):
        server.instance_configs(server._parse_args(['serve'], env), env)


@pytest.fixture
async def running(tmp_path, monkeypatch, caplog):
    env = config(tmp_path)
    # Real stores, auth, settings, gateway and lifespans; no agent or network work.
    async def idle(self, stop, *args):
        await stop.wait()
    monkeypatch.setattr(server.Dispatcher, 'run_forever', idle)
    monkeypatch.setattr(server.Scheduler, 'run_forever', idle)
    def no_codex():
        raise FileNotFoundError('test has no codex')
    monkeypatch.setattr(server, 'codex_binary', no_codex)
    resources = {}
    real_executor = server.RunExecutor
    def capture_executor(store, *args, **kwargs):
        name = 'bss' if str(args[2].data_dir).endswith('/bss') else 'main'
        resources[name] = {'store': store, 'tokens': kwargs['tokens'], 'mcp_url': kwargs['mcp_url']}
        return real_executor(store, *args, **kwargs)
    monkeypatch.setattr(server, 'RunExecutor', capture_executor)
    host = server.create_host(server._parse_args(['serve'], env), env)
    host.state.resources = resources
    async with host.router.lifespan_context(host):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host),
                                     base_url='http://handler') as client:
            yield client, host
    assert not any("instance lifecycle failed" in record.message for record in caplog.records)


def headers(key, user=None):
    return {'Authorization': 'Bearer ' + key, **({'X-Handler-User': user} if user else {})}


async def test_credentials_users_settings_threads_and_webhooks_are_isolated(running):
    client, host = running
    tokens = {}
    for name in ('main', 'bss'):
        base, admin = '/i/' + name, headers(name + '-admin')
        assert (await client.get(base + '/healthz')).json() == {'ok': True}
        assert (await client.get(base + '/api/me', headers=headers(('bss' if name == 'main' else 'main') + '-admin'))).status_code == 401
        assert (await client.post(base + '/api/users', headers=admin,
                    json={'id': 'alice', 'display_name': name + ' Alice'})).status_code == 201
        response = await client.post(base + '/api/users/alice/tokens', headers=admin, json={})
        tokens[name] = response.json()['token']
        response = await client.put(base + '/api/settings/persona', headers=admin,
                                    json={'system': name + ' private system prompt'})
        assert response.status_code == 200
        response = await client.post(base + '/api/chat', headers=headers(tokens[name]),
                                     json={'text': name + ' private chat', 'conversation_id': 'shared-id'})
        assert response.status_code == 200
        from handler.runtime.types import Message, Lane, Role
        store = host.state.resources[name]['store']
        lease, = await store.claim('test', Lane.INTERACTIVE, 1, 60)
        assert await store.add_messages(lease, [Message(lease.thread_key, Role.USER,
            [{'type': 'text', 'text': name + ' transcript'}])])
    for name, other in [('main', 'bss'), ('bss', 'main')]:
        base = '/i/' + name
        assert (await client.get(base + '/api/me', headers=headers(tokens[other]))).status_code == 401
        persona = (await client.get(base + '/api/settings/persona', headers=headers(tokens[name]))).json()
        assert persona['value']['system'] == name + ' private system prompt'
        events = (await client.get(base + '/api/events', headers=headers(tokens[name]))).json()
        assert [e['payload']['text'] for e in events] == [name + ' private chat']
        messages = (await client.get(base + '/api/threads/chat:alice:shared-id/messages',
                                     headers=headers(tokens[name]))).json()
        assert messages[0]['blocks'][0]['text'] == name + ' transcript'
        body = json.dumps({'id': 'notification', 'recipient_user_id': 'alice'}).encode()
        assert (await client.post(base + '/ingest/crontal', content=body,
                    headers={'X-Crontal-Signature': sign(other + '-hook', body)})).status_code == 401
        assert (await client.post(base + '/ingest/crontal', content=body,
                    headers={'X-Crontal-Signature': sign(name + '-hook', body)})).status_code == 202
    assert (await client.get('/api/me', headers=headers(tokens['main']))).status_code == 200
    assert (await client.get('/api/me', headers=headers(tokens['bss']))).status_code == 401
    for path in ('/i/missing/', '/i/missing/api/me', '/i/missing/mcp/'):
        assert (await client.get(path, headers=headers('main-admin'))).status_code == 404


async def test_browser_and_oauth_paths_stay_with_instance(running):
    client, _ = running
    for name in ('main', 'bss'):
        base = '/i/' + name
        page = await client.get(base + '/app')
        assert f'"base": "{base}"' in page.text
        assert f'"instance": "{name}"' in page.text
        assert f'src="{base}/app/static/app.js"' in page.text
        assert (await client.get(base + '/app/static/app.js')).status_code == 200
        assert 'handlerKey' in (await client.get(base + '/login')).text
        callback = await client.get(base + '/oauth/google/callback?error=access_denied')
        assert callback.headers['location'].startswith(base + '/app?')
    status = await client.get('/i/main/api/connections/google', headers=headers('main-admin'))
    assert status.json()['redirect_uri'] == 'https://handler.example/oauth/google/callback'
    assert not (await client.get('/i/bss/api/connections/google', headers=headers('bss-admin'))).json()['configured']


async def test_host_rolls_back_startup_and_closes_concurrently(tmp_path, monkeypatch):
    env = config(tmp_path)
    entered, closed = [], []
    @contextlib.asynccontextmanager
    async def runtime(args, env, *, base_path, instance_name):
        entered.append(instance_name)
        if instance_name == 'bss':
            raise RuntimeError('startup failed')
        try:
            yield server.FastAPI()
        finally:
            closed.append(instance_name)
    monkeypatch.setattr(server, 'instance_runtime', runtime)
    host = server.create_host(server._parse_args(['serve'], env), env)
    with pytest.raises(RuntimeError, match='startup failed'):
        async with host.router.lifespan_context(host):
            pass
    assert entered == ['main', 'bss'] and closed == ['main']

    drained = asyncio.Event()
    @contextlib.asynccontextmanager
    async def runtime(args, env, *, base_path, instance_name):
        try:
            yield server.FastAPI()
        finally:
            closed.append(instance_name)
            if instance_name == 'main':
                drained.set()
            await asyncio.wait_for(drained.wait(), .5)
    monkeypatch.setattr(server, 'instance_runtime', runtime)
    host = server.create_host(server._parse_args(['serve'], env), env)
    async with host.router.lifespan_context(host):
        pass
    assert set(closed[-2:]) == {'bss', 'main'}


def test_env_file_and_harness_credentials_are_snapshots(tmp_path, monkeypatch):
    path = tmp_path / 'harness.env'
    path.write_text('CODEX_API_KEY=file-key\n')
    before = dict(os.environ)
    assert server.read_env_file(path) == {'CODEX_API_KEY': 'file-key'}
    assert dict(os.environ) == before
    env = {'CODEX_API_KEY': 'first-key'}
    harness = CodexHarness('unused', environment=env)
    env['CODEX_API_KEY'] = 'changed-key'
    monkeypatch.setenv('CODEX_API_KEY', 'host-key')
    assert harness.environment['CODEX_API_KEY'] == 'first-key'
    assert SecretBox.from_env({'HANDLER_SECRET_KEY': 'one'}).verify(
        SecretBox.from_env({'HANDLER_SECRET_KEY': 'two'}).sign({'x': 1}, 60)) is None


async def test_mcp_run_tokens_and_internal_urls_are_isolated(running):
    from handler.runtime.context import RunContext
    client, host = running
    context = RunContext('same-run', 'chat:alice:same-thread', 'alice')
    token = host.state.resources['main']['tokens'].issue(context, 60)
    body = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2024-11-05', 'capabilities': {},
        'clientInfo': {'name': 'test', 'version': '1'}}}
    request_headers = {**headers(token), 'Accept': 'application/json, text/event-stream'}
    for path in ('/i/main/mcp/', '/mcp/'):
        response = await client.post(path, headers=request_headers, json=body)
        assert response.status_code == 200, response.text
    assert (await client.post('/i/bss/mcp/', headers=request_headers, json=body)).status_code == 401
    assert host.state.resources['main']['mcp_url'].endswith('/i/main/mcp/')
    assert host.state.resources['bss']['mcp_url'].endswith('/i/bss/mcp/')


@pytest.mark.parametrize('instances', ['', 'main'])
async def test_single_instance_preserves_root_routes(tmp_path, monkeypatch, instances):
    env = config(tmp_path)
    env['HANDLER_INSTANCES'] = instances
    async def idle(self, stop, *args):
        await stop.wait()
    monkeypatch.setattr(server.Dispatcher, 'run_forever', idle)
    monkeypatch.setattr(server.Scheduler, 'run_forever', idle)
    monkeypatch.setattr(server, 'codex_binary', lambda: 'unused')
    host = server.create_host(server._parse_args(['serve'], env), env)
    async with host.router.lifespan_context(host):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=host), base_url='http://handler') as client:
            assert (await client.get('/healthz')).status_code == 200
            assert (await client.get('/api/me', headers=headers('main-admin'))).status_code == 200
            page = await client.get('/app')
            assert 'src="/app/static/app.js"' in page.text
            if instances:
                assert (await client.get('/i/main/api/me', headers=headers('main-admin'))).status_code == 200
