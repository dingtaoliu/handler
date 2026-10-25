"""Least privilege on threads fed by outside input (email, crontal notifications)."""

from __future__ import annotations

from pathlib import Path

import pytest

from handler.runtime.context import RunContext
from handler.runtime.gateway import GrantPolicy, ToolSpec
from handler.runtime.gateway.crontal import CRONTAL_TOOLS
from handler.runtime.gateway.crontal_api import CrontalApiTools, outward_call
from handler.runtime.types import Decision, Risk

# The Heroku instance's grants at the time of the audit.
PROD = {"actions": {"crontal.*": "allow", "crontal_api.*": "allow"}}
EMAIL = RunContext(run_id="r", thread_key="email:mallory@evil.example:root@x")
CRONTAL = RunContext(run_id="r", thread_key="crontal:bid:7")
CHAT = RunContext(run_id="r", thread_key="chat:1:abc")


def spec(action, risk=Risk.READ, **kwargs):
    return ToolSpec(action.replace(".", "_"), action, risk, "", {"type": "object"},
                    handler=None, **kwargs)


@pytest.fixture
def call_spec(tmp_path: Path) -> ToolSpec:
    tools = CrontalApiTools("http://crontal", "t", workspace_for=lambda key: tmp_path)
    return {s.name: s for s in tools.tools()}["crontal_api_call"]


INSIDE = [
    ("POST", "/api/v1/rfq", {"name": "316L flat bar"}),
    ("POST", "/api/v1/rfq", {"name": "x", "status": "draft"}),
    ("PUT", "/api/v1/rfq/5", {"name": "y", "status": "DRAFT"}),
    ("POST", "/api/v1/demand_listing", {"rfq_id": 5}),
    ("PUT", "/api/v1/demand_listing/9", {"quantity": 3}),
    ("POST", "/api/v1/bid/start", {"rfq_id": 5}),
    ("PUT", "/api/v1/bid/3", {"terms": "FOB"}),
    ("POST", "/api/v1/bid/3/upload", None),
    ("POST", "/api/v1/supply_listing", {"product_id": 2}),
    ("POST", "/api/v1/files/upload", None),
    ("POST", "/api/v1/contact", {"name": "Wei"}),
    ("PATCH", "/api/v1/operations/partners/4", {"notes": "x"}),
    ("POST", "/api/v1/notification/12/read", None),
]
OUTWARD = [
    ("POST", "/api/v1/rfq", {"name": "x", "status": "published"}),
    ("PUT", "/api/v1/rfq/5", {"status": "PUBLISHED"}),
    ("PUT", "/api/v1/rfq/5", {"status": "closed"}),
    ("POST", "/api/v1/rfq/5/share-email", {"contact_list_id": 1}),
    ("POST", "/api/v1/bid/3/lock", {}),
    ("POST", "/api/v1/bid/token/abc/revisions", {}),
    ("POST", "/api/v1/bid/3/respond", {"response_type": "agree"}),
    ("POST", "/api/v1/chat", {"merchant_id": 9}),
    ("POST", "/api/v1/chat/scoped", {}),
    ("POST", "/api/v1/message/chat/4", {"content": "hi"}),
    ("PUT", "/api/v1/message/8", {"content": "hi"}),
    ("POST", "/api/v1/merchant/2/invites", {"email": "a@b.example"}),
    ("PUT", "/api/v1/merchant/2", {"name": "x"}),
    ("PUT", "/api/v1/card/me", {}),
    ("POST", "/api/v1/auth/forgot-password", {"email": "a@b.example"}),
    ("POST", "/api/v1/agent/chat", {"message": "publish"}),
    ("POST", "/api/v1/something-new", {}),  # unknown areas count as outward
    ("POST", "/api/v1/rfq/5/publish", {}),  # future route caught by keyword
    ("POST", "/api/v1/RFQ/5", {}),  # not a known inside path
]


@pytest.mark.parametrize("method, path, body", INSIDE)
def test_inside_writes_run_on_untrusted_threads(call_spec, method, path, body):
    args = {"method": method, "path": path, "json": body}
    assert outward_call(args) is None
    policy = GrantPolicy(PROD)
    for ctx in (EMAIL, CRONTAL, CHAT):
        assert policy.decide(ctx, call_spec, args)[0] == Decision.ALLOW


@pytest.mark.parametrize("method, path, body", OUTWARD)
def test_outward_writes_need_approval_on_untrusted_threads(call_spec, method, path, body):
    args = {"method": method, "path": path, "json": body}
    assert outward_call(args)
    policy = GrantPolicy(PROD)
    for ctx in (EMAIL, CRONTAL):
        decision, reason = policy.decide(ctx, call_spec, args)
        assert decision == Decision.ASK and "outside input" in reason
    # Chat keeps the instance rules.
    assert policy.decide(CHAT, call_spec, args)[0] == Decision.ALLOW
    # An exact instance rule doesn't lift the floor either; deny stays deny.
    exact = GrantPolicy({"actions": {"crontal_api.call": "allow"}})
    assert exact.decide(EMAIL, call_spec, args)[0] == Decision.ASK
    denied = GrantPolicy({"actions": {"crontal_api.call": "deny"}})
    assert denied.decide(EMAIL, call_spec, args)[0] == Decision.DENY


def test_status_in_a_multipart_form_counts_too():
    assert outward_call({"method": "PUT", "path": "/api/v1/rfq/5",
                         "form": {"status": "published"}})


def test_reads_stay_allowed(call_spec):
    policy = GrantPolicy(PROD)
    assert policy.decide(EMAIL, spec("crontal_api.get"), {"path": "/api/v1/bid/3"})[0] \
        == Decision.ALLOW
    assert policy.decide(EMAIL, spec("gmail.search"), {})[0] == Decision.ALLOW


def test_cross_thread_writes_exec_and_mail_need_approval():
    policy = GrantPolicy({"actions": {"memory.*": "allow", "automation.*": "allow",
                                      "gmail.send": "allow", "shell.*": "allow"}})
    for action in ("memory.save", "memory.rewrite", "memory.delete", "automation.create",
                   "automation.delete"):
        s = spec(action, Risk.WRITE_INTERNAL)
        assert policy.decide(EMAIL, s, {})[0] == Decision.ASK
        assert policy.decide(CRONTAL, s, {})[0] == Decision.ASK
        assert policy.decide(CHAT, s, {})[0] == Decision.ALLOW
    send = spec("gmail.send", Risk.WRITE_EXTERNAL, explicit_allow_only=True)
    assert policy.decide(EMAIL, send, {})[0] == Decision.ASK
    assert policy.decide(CHAT, send, {})[0] == Decision.ALLOW
    assert GrantPolicy().decide(EMAIL, spec("shell.run", Risk.EXEC), {})[0] == Decision.ASK
    assert policy.decide(EMAIL, spec("shell.run", Risk.EXEC), {})[0] == Decision.ASK
    # Work that stays in the thread or the merchant still runs.
    for action in ("thread.set_title", "notify.send", "gmail.create_draft",
                   "gmail.update_draft"):
        assert GrantPolicy().decide(EMAIL, spec(action, Risk.WRITE_INTERNAL), {})[0] \
            == Decision.ALLOW


def test_a_failing_classifier_asks():
    def broken(args):
        raise KeyError("boom")

    s = spec("x.call", Risk.WRITE_EXTERNAL, outward=broken)
    assert GrantPolicy({"actions": {"x.*": "allow"}}).decide(EMAIL, s, {})[0] == Decision.ASK


def test_curated_crontal_tools_are_classified():
    policy = GrantPolicy(PROD)

    def curated(name):
        cfg = CRONTAL_TOOLS[name]
        return spec(f"crontal.{cfg['rename']}", Risk(cfg["risk"]), outward=cfg.get("outward"))

    assert policy.decide(EMAIL, curated("app_create_rfq"), {"name": "x"})[0] == Decision.ALLOW
    assert policy.decide(EMAIL, curated("app_update_rfq"),
                         {"rfq_id": 1, "status": "published"})[0] == Decision.ASK
    assert policy.decide(EMAIL, curated("app_update_rfq"),
                         {"rfq_id": 1, "fields": {"status": "closed"}})[0] == Decision.ASK
    assert policy.decide(EMAIL, curated("app_publish_rfq"), {"rfq_id": 1})[0] == Decision.ASK
    assert policy.decide(EMAIL, curated("app_add_demand_listings"), {})[0] == Decision.ALLOW
    assert policy.decide(EMAIL, curated("app_define_shape"), {})[0] == Decision.ASK
    assert policy.decide(CHAT, curated("app_publish_rfq"), {})[0] == Decision.ALLOW
