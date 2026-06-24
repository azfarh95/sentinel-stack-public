"""P4.1 + P4.3 gating logic test (no real browser). Patches the parent Tools.act
to a recorder, then asserts: state-change + deny → declined + parent NOT run; read →
no prompt + delegates; allow → delegates; and the P4.3 domain guard on navigate
(blocked → auto-deny, sensitive → prompt, normal → pass)."""
import asyncio

import browser_use
from browser_use import ActionResult
from domain_guard import DomainPolicy
from tools_gated import STATE_CHANGING, GatedTools

_calls = {"delegated": 0}


async def _fake_parent_act(self, action, browser_session, *a, **k):
    _calls["delegated"] += 1
    return ActionResult(extracted_content="parent-ran")


browser_use.tools.service.Tools.act = _fake_parent_act


class FakeAction:
    def __init__(self, name, params):
        self._n, self._p = name, params

    def model_dump(self, exclude_unset=True):
        return {self._n: self._p}


async def main():
    pol = DomainPolicy(sensitive=["mybank.com"], blocked=["evil.com"])

    # 1) state-changing + DENY → declined, parent not run
    seen = []
    async def deny(name, params, page=None):
        seen.append((name, page)); return False
    gt = GatedTools(approve=deny, policy=pol)
    r = await gt.act(FakeAction("click", {"index": 1}), None)
    assert seen and seen[0][0] == "click", seen
    assert "DECLINED" in (r.extracted_content or "")
    assert _calls["delegated"] == 0, "parent must NOT run when denied"

    # 2) read (scroll) → no prompt, delegates
    await gt.act(FakeAction("scroll", {"down": True}), None)
    assert len(seen) == 1, "a read must not prompt"
    assert _calls["delegated"] == 1

    # 3) state-changing + ALLOW → prompt, delegates
    seen2 = []
    async def allow(name, params, page=None):
        seen2.append((name, page)); return True
    gt2 = GatedTools(approve=allow, policy=pol)
    await gt2.act(FakeAction("input", {"text": "hi"}), None)
    assert seen2 == [("input", None)], seen2          # no browser → page None, still gated
    assert _calls["delegated"] == 2

    # 4) P4.3 domain guard — navigate BLOCKED → auto-deny, no prompt, parent not run
    seen3 = []
    async def rec(name, params, page=None):
        seen3.append((name, page)); return True       # would allow, but blocked shouldn't ask
    gt3 = GatedTools(approve=rec, policy=pol)
    r = await gt3.act(FakeAction("navigate", {"url": "https://evil.com/x"}), None)
    assert "BLOCKED" in (r.extracted_content or ""), r.extracted_content
    assert seen3 == [], "blocked nav must not prompt"
    assert _calls["delegated"] == 2, "blocked nav must not delegate"

    # 5) navigate SENSITIVE → prompts (with the domain); deny → declined
    seen4 = []
    async def deny4(name, params, page=None):
        seen4.append((name, page)); return False
    gt4 = GatedTools(approve=deny4, policy=pol)
    r = await gt4.act(FakeAction("navigate", {"url": "https://login.mybank.com/"}), None)
    assert seen4 == [("navigate", "login.mybank.com")], seen4
    assert "DECLINED" in (r.extracted_content or "")
    assert _calls["delegated"] == 2

    # 6) navigate NORMAL → no prompt, delegates
    await gt4.act(FakeAction("navigate", {"url": "https://example.com/"}), None)
    assert seen4 == [("navigate", "login.mybank.com")], "normal nav must not prompt"
    assert _calls["delegated"] == 3

    print("PASS — gate (deny/read/allow) + domain guard (blocked/sensitive/normal) all correct.")
    print("gated actions:", sorted(STATE_CHANGING))


asyncio.run(main())
