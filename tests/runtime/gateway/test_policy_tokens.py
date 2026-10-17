from datetime import datetime, timedelta, timezone

from handler.runtime.context import RunContext
from handler.runtime.gateway import GrantPolicy, RunTokens, ToolSpec
from handler.runtime.types import Decision, Risk

CTX = RunContext(run_id="r1", thread_key="t1")


def spec(action, risk=Risk.READ):
    return ToolSpec("x", action, risk, "", {"type": "object"}, handler=None)


def test_risk_defaults_and_fallback():
    policy = GrantPolicy({"defaults": {"read": "allow", "write_internal": "allow",
                                       "write_external": "ask", "exec": "deny"}})
    assert policy.decide(CTX, spec("a.b"), {})[0] == Decision.ALLOW
    assert policy.decide(CTX, spec("a.b", Risk.WRITE_EXTERNAL), {})[0] == Decision.ASK
    assert policy.decide(CTX, spec("a.b", Risk.EXEC), {})[0] == Decision.DENY
    # no config at all -> built-in defaults
    assert GrantPolicy({}).decide(CTX, spec("a.b", Risk.EXEC), {})[0] == Decision.ASK
    # missing risk entry -> ask
    bare = GrantPolicy()
    bare._defaults = {}
    assert bare.decide(CTX, spec("a.b"), {})[0] == Decision.ASK


def test_precedence_exact_then_longest_glob_then_default():
    policy = GrantPolicy({"actions": {
        "gmail.*": "ask", "gmail.read.*": "allow", "gmail.read.secret": "deny", "*": "deny",
    }})
    assert policy.decide(CTX, spec("gmail.read.secret"), {})[0] == Decision.DENY
    assert policy.decide(CTX, spec("gmail.read.inbox"), {})[0] == Decision.ALLOW
    assert policy.decide(CTX, spec("gmail.send"), {})[0] == Decision.ASK
    assert policy.decide(CTX, spec("other.thing"), {})[0] == Decision.DENY
    # exact beats a longer glob
    p2 = GrantPolicy({"actions": {"memory.save": "ask", "memory.s*": "allow"}})
    assert p2.decide(CTX, spec("memory.save"), {})[0] == Decision.ASK
    # risk default when no action rule matches
    p3 = GrantPolicy({"actions": {"memory.*": "allow"}})
    assert p3.decide(CTX, spec("gmail.send", Risk.WRITE_EXTERNAL), {})[0] == Decision.ASK


def test_tokens_issue_resolve_expire_revoke():
    now = [datetime(2026, 1, 1, tzinfo=timezone.utc)]
    tokens = RunTokens(clock=lambda: now[0])
    token = tokens.issue(CTX, 60)
    assert len(token) >= 32
    assert tokens.resolve(token) == CTX
    assert tokens.resolve("nope") is None and tokens.resolve(None) is None
    other = tokens.issue(CTX, 60)
    assert other != token
    tokens.revoke(other)
    assert tokens.resolve(other) is None
    now[0] += timedelta(seconds=61)
    assert tokens.resolve(token) is None
