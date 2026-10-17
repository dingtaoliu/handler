from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock

import pytest

from handler.runtime.context import RunContext
from handler.runtime.scheduler import Scheduler
from handler.runtime.tools.automation import AutomationTools
from handler.runtime.types import Risk


@pytest.fixture
def tools(store, clock):
    scheduler = Scheduler(store, clock=clock)
    scheduler.wake = Mock()
    provider = AutomationTools(store, scheduler)
    return {spec.name: spec for spec in provider.tools()}, scheduler


def test_tool_specs(tools):
    specs, _ = tools
    assert list(specs) == ["automation_create", "automation_list", "automation_delete"]
    for name, spec in specs.items():
        assert spec.action == name.replace("_", ".")
        assert spec.input_schema["type"] == "object"
    assert specs["automation_create"].risk == Risk.WRITE_INTERNAL
    assert specs["automation_list"].risk == Risk.READ
    assert specs["automation_delete"].risk == Risk.WRITE_INTERNAL
    assert specs["automation_create"].input_schema["required"] == ["name", "prompt", "schedule"]


@pytest.mark.parametrize("acting_as,user_id,creator", [("delegate", "u", "delegate"), (None, "u", "u"), (None, None, None)])
async def test_create_defaults_and_authority(store, clock, tools, acting_as, user_id, creator):
    specs, scheduler = tools
    ctx = RunContext("run", "chat:u:c", user_id=user_id, acting_as=acting_as)
    result = await specs["automation_create"].handler(ctx, {
        "name": "Digest", "prompt": "Summarize", "schedule": "every 30m",
    })
    assert not result.is_error
    schedule = await store.get_schedule(result.content["schedule_id"])
    assert schedule.name == "Digest" and schedule.prompt == "Summarize"
    assert schedule.spec == "every 30m" and schedule.timezone == "UTC" and schedule.notify == []
    assert schedule.created_by == creator and schedule.created_at == clock()
    assert schedule.one_shot is False
    assert schedule.next_run_at == clock() + timedelta(minutes=30)
    assert result.content["next_run_at"] == schedule.next_run_at.isoformat()
    scheduler.wake.assert_called_once_with()


async def test_create_one_shot_with_timezone_and_notify(store, clock, tools):
    specs, scheduler = tools
    result = await specs["automation_create"].handler(RunContext("run", "chat:u:c"), {
        "name": "One shot", "prompt": "Check", "schedule": "at: 2026-10-18T09:00:00",
        "timezone": "America/New_York", "notify": ["u", "v"],
    })
    assert not result.is_error
    schedule = await store.get_schedule(result.content["schedule_id"])
    assert schedule.one_shot is True
    assert schedule.notify == ["u", "v"]
    assert result.content["next_run_at"] == "2026-10-18T13:00:00+00:00"
    scheduler.wake.assert_called_once_with()


@pytest.mark.parametrize("args", [
    {"schedule": "bad"}, {"schedule": "every 0s"},
    {"schedule": "cron: 61 * * * *"}, {"schedule": "at: oops"},
    {"schedule": "every 1h", "timezone": "Nowhere/Nope"},
])
async def test_create_invalid_spec_has_no_effect(store, tools, args):
    specs, scheduler = tools
    result = await specs["automation_create"].handler(RunContext("run", "thread"), {
        "name": "Bad", "prompt": "p", **args,
    })
    assert result.is_error
    assert await store.list_schedules(include_disabled=True) == []
    scheduler.wake.assert_not_called()


async def test_list_and_delete(store, clock, tools):
    specs, scheduler = tools
    ctx = RunContext("run", "chat:u:c", user_id="u")
    assert (await specs["automation_list"].handler(ctx, {})).content == []
    result = await specs["automation_create"].handler(ctx, {
        "name": "Digest", "prompt": "Summarize", "schedule": "every 1h",
    })
    schedule_id = result.content["schedule_id"]
    schedule = await store.get_schedule(schedule_id)
    await store.update_schedule(replace(schedule, enabled=False))
    listed = await specs["automation_list"].handler(ctx, {})
    assert listed.content == [{
        "id": schedule_id, "name": "Digest", "spec": "every 1h",
        "next_run_at": (clock() + timedelta(hours=1)).isoformat(), "enabled": False,
    }]
    scheduler.wake.reset_mock()
    deleted = await specs["automation_delete"].handler(ctx, {"schedule_id": schedule_id})
    assert deleted.content == {"schedule_id": schedule_id, "deleted": True}
    assert await store.get_schedule(schedule_id) is None
    scheduler.wake.assert_called_once_with()
    missing = await specs["automation_delete"].handler(ctx, {"schedule_id": schedule_id})
    assert missing.content["deleted"] is False
