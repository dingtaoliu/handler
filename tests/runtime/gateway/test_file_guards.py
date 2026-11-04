"""Deleting Crontal files always needs approval, whatever the grants."""

from __future__ import annotations

from pathlib import Path

import pytest

from handler.runtime.context import RunContext
from handler.runtime.gateway import GrantPolicy, ToolSpec
from handler.runtime.gateway.crontal_api import CrontalApiTools, destructive_call
from handler.runtime.types import Decision, Risk

PROD = {"actions": {"crontal.*": "allow", "crontal_api.*": "allow"}}
EXACT = {"actions": {"crontal_api.call": "allow"}}
CHAT = RunContext(run_id="r", thread_key="chat:1:abc")
TASK = RunContext(run_id="r", thread_key="task:9")
EMAIL = RunContext(run_id="r", thread_key="email:mallory@evil.example:root@x")


@pytest.fixture
def call_spec(tmp_path: Path) -> ToolSpec:
    tools = CrontalApiTools("http://crontal", "t", workspace_for=lambda key: tmp_path)
    return {s.name: s for s in tools.tools()}["crontal_api_call"]


DESTRUCTIVE = [
    ("DELETE", "/api/v1/files/12"),
    ("DELETE", "/api/v1/files/12?permanent=true"),
    ("delete", "/API/v1/files/12"),
    ("POST", "/api/v1/files/bulk/trash"),
    ("POST", "/api/v1/files/bulk/trash/"),
    ("POST", "/api/v1/files/bulk/delete"),
    ("POST", "/api/v1/files/trash/empty"),
]
ORGANISE = [
    ("POST", "/api/v1/files/upload"),
    ("PATCH", "/api/v1/files/12"),
    ("POST", "/api/v1/files/folders"),
    ("POST", "/api/v1/files/bulk/move"),
    ("POST", "/api/v1/files/bulk/restore"),
    ("POST", "/api/v1/files/bulk/download"),
    ("DELETE", "/api/v1/bid/3/files/4"),  # detaches a quote attachment only
    ("DELETE", "/api/v1/filesystem/1"),
]


@pytest.mark.parametrize("method,path", DESTRUCTIVE)
@pytest.mark.parametrize("grants", [PROD, EXACT])
@pytest.mark.parametrize("ctx", [CHAT, TASK, EMAIL])
def test_file_deletes_always_ask(call_spec, method, path, grants, ctx):
    args = {"method": method, "path": path, "json": {"ids": [1]}}
    assert destructive_call(args)
    decision, reason = GrantPolicy(grants).decide(ctx, call_spec, args)
    assert decision == Decision.ASK
    assert "approval needed" in reason


@pytest.mark.parametrize("method,path", ORGANISE)
def test_organising_files_follows_the_grants(call_spec, method, path):
    args = {"method": method, "path": path, "json": {"ids": [1]}}
    assert destructive_call(args) is None
    decision, _ = GrantPolicy(PROD).decide(CHAT, call_spec, args)
    assert decision == Decision.ALLOW


def test_deny_stays_deny(call_spec):
    policy = GrantPolicy({"actions": {"crontal_api.call": "deny"}})
    decision, _ = policy.decide(CHAT, call_spec, {"method": "DELETE",
                                                 "path": "/api/v1/files/1"})
    assert decision == Decision.DENY


def test_a_failing_classifier_asks():
    def broken(args):
        raise RuntimeError("bug")

    spec = ToolSpec("x_call", "x.call", Risk.WRITE_INTERNAL, "", {"type": "object"},
                    handler=None, always_ask=broken)
    decision, reason = GrantPolicy({"actions": {"x.*": "allow"}}).decide(CHAT, spec, {})
    assert decision == Decision.ASK and "could not tell" in reason


def test_listing_decision_without_args_is_unchanged(call_spec):
    # The admin tool list asks the policy with no arguments.
    assert GrantPolicy(PROD).decide(CHAT, call_spec, {})[0] == Decision.ALLOW
