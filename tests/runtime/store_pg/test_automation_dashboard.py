from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI

from handler.runtime.auth import Authenticator
from handler.runtime.settings import InstanceDefaults, SettingsService
from handler.runtime.store.sqlite_users import User
from handler.runtime.types import Schedule
from handler.runtime.web.automation_dashboard import automation_dashboard_router
from .test_automation_metrics import add_run


async def test_dashboard_paused_never_run_deleted_defaults_and_auth(domain_store, clock, monkeypatch):
    store = domain_store
    monkeypatch.setattr('handler.runtime.web.automation_dashboard.utcnow', clock)
    await store.create_user(User('member', 'Member', 'member'))
    _, token = await store.create_token('member')
    settings = SettingsService(store, defaults=InstanceDefaults(model='gpt-6.1-terra'))
    active = Schedule('Hourly', 'Check new inquiries', 'every 1h', created_by='member',
                      next_run_at=clock()+timedelta(hours=1), created_at=clock())
    paused = Schedule('Paused', 'Do not run', 'every 1d', enabled=False, created_at=clock())
    await store.create_schedule(active)
    await store.create_schedule(paused)
    await add_run(store, clock, 'automation:'+active.id, cost=.5)
    await add_run(store, clock, 'automation:deleted', cost=.25)
    app = FastAPI()
    app.include_router(automation_dashboard_router(store, Authenticator(store), settings))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
        assert (await client.get('/api/automations/dashboard')).status_code == 401
        h={'Authorization':'Bearer '+token}
        response = await client.get('/api/automations/dashboard',headers=h)
        assert response.status_code == 200, response.text
        body=response.json()
        assert body['totals']['automations']==2 and body['totals']['enabled']==1
        assert body['totals']['deleted']==1 and body['totals']['runs']==2
        assert body['totals']['known_cost_usd']==pytest.approx(.75)
        rows={r['id']:r for r in body['items']}
        assert rows[active.id]['metrics']['runs']==1
        # Existing thread model wins, just like execution; untouched schedules inherit default.
        assert rows[active.id]['effective_model']=='test-model'
        assert rows[paused.id]['effective_model']=='gpt-6.1-terra'
        assert rows[paused.id]['metrics']['runs']==0
        assert rows[paused.id]['metrics']['known_cost_usd']==0
        assert rows[paused.id]['next_run_at'] is None
        assert rows['deleted']['deleted'] and rows['deleted']['prompt'] is None
        for bad in ('0','-1','3651','nope'):
            assert (await client.get('/api/automations/dashboard',params={'days':bad},headers=h)).status_code==422
        clock.advance(2*86400)
        recent=(await client.get('/api/automations/dashboard?days=1',headers=h)).json()
        assert recent['totals']['runs']==0
        assert len(recent['items'])==2  # both current schedules remain, even without recent runs
        assert recent['since']
