"""P4.1 — approval-gated, scoped tool-set for the browser assistant.

`GatedTools` subclasses browser-use's `Tools` and requires owner approval before
any STATE-CHANGING action (click / type / submit / dropdown / send-keys / upload /
file-write / file-replace / save-pdf / arbitrary JS-`evaluate` / drag), while reads
(extract, scroll, screenshot, find_*, read_file, wait, done) pass straight through.

The approval CHANNEL is injected — a console y/N prompt by default (CLI / standalone
use); P4.2 swaps in a Telegram owner-in-the-loop channel. Gating is CENTRAL: we
override `act()` and delegate to the parent for everything allowed, so browser-use's
real action handlers are reused (no reimplementation).

Also folds in the P1d selector-based real-CDP drag (`drag_selector`) — itself gated.
"""
# NB: no `from __future__ import annotations` — browser-use's action-signature
# normalizer needs the REAL BrowserSession type (not a string) on drag_selector,
# or it mis-flags the auto-injected browser_session arg as a conflict.
import asyncio
import json
import urllib.request

from browser_use import ActionResult, BrowserSession, Tools

# The shopping MCP's REST shim (convergence P1) — the anti-bot nodriver scrapers
# live there; the agent calls this instead of trying to browse Shopee/Lazada
# directly (which their bot-detection would block).
SHOPPING_API = "http://127.0.0.1:8100/api/search"

from domain_guard import DomainPolicy, domain_of

try:                                  # log() is best-effort (JSONL run log)
    from agent_runner import log
except Exception:                     # pragma: no cover
    def log(rec):  # type: ignore
        pass

# World-changing / dangerous actions — require approval before they execute.
# (Navigation — navigate/search/go_back — is handled by the P4.3 domain guard,
# not gated here, so read-only browsing stays frictionless.)
STATE_CHANGING: set[str] = {
    "click", "input", "select_dropdown", "send_keys", "upload_file",
    "evaluate", "write_file", "replace_file", "save_as_pdf", "drag_selector",
}


def _short(params, n: int = 200) -> str:
    try:
        return json.dumps(params, ensure_ascii=False)[:n]
    except Exception:
        return str(params)[:n]


async def console_approve(name: str, params, page=None) -> bool:
    """Default approval channel — y/N on the console (CLI / standalone runs)."""
    where = f" on {page}" if page else ""
    prompt = f"\n[APPROVE] {name}{where} ({_short(params)}) — allow? [y/N] "
    loop = asyncio.get_event_loop()
    ans = (await loop.run_in_executor(None, lambda: input(prompt)) or "").strip().lower()
    return ans in ("y", "yes")


async def auto_deny(name: str, params, page=None) -> bool:
    """A channel that declines everything — safe default for unattended runs."""
    return False


async def _eval(browser_session, expr):
    cdp = await browser_session.get_or_create_cdp_session()
    r = await cdp.cdp_client.send.Runtime.evaluate(
        {"expression": expr, "returnByValue": True}, session_id=cdp.session_id)
    return r.get("result", {}).get("value")


class GatedTools(Tools):
    """browser-use Tools that gate STATE_CHANGING actions behind
    `approve(name, params) -> bool`. Reads delegate straight to the parent."""

    def __init__(self, *args, approve=None, policy=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._approve = approve or console_approve
        self._policy = policy or DomainPolicy.load()
        self._register_extra()

    def _declined(self, name: str, why: str = "DECLINED by the owner") -> ActionResult:
        msg = (f"Action '{name}' was {why}. Do NOT retry it — choose a different "
               "approach or finish the task.")
        log({"label": "gate-declined", "action": name, "why": why})
        return ActionResult(extracted_content=msg, long_term_memory=msg)

    async def _gate(self, name, params, page=None) -> bool:
        try:
            ok = bool(await self._approve(name, params, page=page))
        except Exception:
            ok = False
        log({"label": "gate-approved" if ok else "gate-denied", "action": name, "page": page})
        return ok

    async def act(self, action, browser_session, *args, **kwargs) -> ActionResult:
        chosen = None
        try:
            for nm, pr in action.model_dump(exclude_unset=True).items():
                if pr is not None:
                    chosen = (nm, pr)
                    break
        except Exception:
            chosen = None

        if chosen:
            name, params = chosen
            # P4.3 — domain guard on navigation
            if name == "navigate":
                url = params.get("url", "") if isinstance(params, dict) else ""
                klass = self._policy.classify(url)
                if klass == "blocked":
                    return self._declined(name, why=f"BLOCKED by policy (domain {domain_of(url)})")
                if klass == "sensitive" and not await self._gate(name, params, page=domain_of(url)):
                    return self._declined(name)
            # P4.1 — state-change gate, enriched with the current page's domain
            elif name in STATE_CHANGING:
                page = None
                try:
                    page = domain_of(await _eval(browser_session, "location.href"))
                except Exception:
                    page = None
                if not await self._gate(name, params, page=page):
                    return self._declined(name)

        return await super().act(action, browser_session, *args, **kwargs)

    def _register_extra(self) -> None:
        @self.registry.action(
            "Drag the element matching from_selector and drop it onto to_selector using a real "
            "mouse-pointer drag. Pass CSS selectors (e.g. '#a'). USE for HTML5 / jQuery-UI "
            "drag-and-drop + sortable lists where the draggables are NOT in the indexed list. "
            "Do NOT use evaluate()/JavaScript for the drag.")
        async def drag_selector(from_selector: str, to_selector: str, browser_session: BrowserSession):
            coords = await _eval(browser_session, f"""(function(){{
                function c(sel){{var e=document.querySelector(sel); if(!e) return null;
                    var r=e.getBoundingClientRect(); return {{x:r.x+r.width/2, y:r.y+r.height/2}};}}
                return JSON.stringify({{a:c({json.dumps(from_selector)}), b:c({json.dumps(to_selector)})}});
            }})()""")
            try:
                pos = json.loads(coords) if coords else {}
            except Exception:
                pos = {}
            a, b = pos.get("a"), pos.get("b")
            if not a or not b:
                return ActionResult(error=f"drag_selector: could not locate {from_selector} or {to_selector}")
            cdp = await browser_session.get_or_create_cdp_session()
            client, sid = cdp.cdp_client, cdp.session_id
            sx, sy, tx, ty = a["x"], a["y"], b["x"], b["y"]
            await client.send.Input.dispatchMouseEvent({"type": "mouseMoved", "x": sx, "y": sy}, session_id=sid)
            await client.send.Input.dispatchMouseEvent(
                {"type": "mousePressed", "x": sx, "y": sy, "button": "left", "clickCount": 1}, session_id=sid)
            await asyncio.sleep(0.12)
            n = 12
            for i in range(1, n + 1):
                ix, iy = sx + (tx - sx) * i / n, sy + (ty - sy) * i / n
                await client.send.Input.dispatchMouseEvent(
                    {"type": "mouseMoved", "x": ix, "y": iy, "button": "left"}, session_id=sid)
                await asyncio.sleep(0.03)
            await asyncio.sleep(0.12)
            await client.send.Input.dispatchMouseEvent(
                {"type": "mouseReleased", "x": tx, "y": ty, "button": "left", "clickCount": 1}, session_id=sid)
            await asyncio.sleep(0.3)
            msg = f"drag_selector {from_selector} -> {to_selector} done."
            return ActionResult(extracted_content=msg, long_term_memory=msg)

        @self.registry.action(
            "Search Singapore shopping marketplaces (Shopee, Lazada, Amazon SG, Challenger and "
            "other Shopify stores) for a product and get back PRICE-SORTED listings. USE THIS for "
            "any find / compare / cheapest / price-of <product> task INSTEAD of navigating to those "
            "sites yourself — they have anti-bot protection that this dedicated backend handles. "
            "marketplaces: 'all' (default) or a comma list like 'shopee.sg,lazada.sg'. "
            "top_n: listings per source (default 8). This is read-only.")
        async def shopping_search(query: str, marketplaces: str = "all", top_n: int = 8):
            payload = json.dumps({"query": query, "marketplaces": marketplaces,
                                  "top_n": int(top_n)}).encode()

            def _call():
                req = urllib.request.Request(SHOPPING_API, data=payload,
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    return json.loads(r.read().decode())

            try:
                data = await asyncio.get_event_loop().run_in_executor(None, _call)
            except Exception as e:                       # backend down / timeout
                return ActionResult(error=f"shopping_search failed: {type(e).__name__}: {str(e)[:160]}")
            if not data.get("ok", True):
                return ActionResult(error=f"shopping_search: {data.get('error', 'unknown error')}")

            listings = data.get("listings", [])[:15]
            lines = []
            for i, l in enumerate(listings, 1):
                price = l.get("price_sgd")
                price_s = f"S${price}" if price is not None else "price?"
                lines.append(f"{i}. {price_s} — {str(l.get('title', '?'))[:70]} "
                             f"[{l.get('marketplace', '?')}] {l.get('url', '')}")
            summary = (f"Found {data.get('count', 0)} listings across "
                       f"{', '.join(data.get('sources_queried', [])) or 'no sources'} "
                       f"(cheapest first):\n" + ("\n".join(lines) or "(none)"))
            issues = data.get("issues") or []
            if issues:
                summary += ("\nNOTE — some sources were blocked/failed (results not exhaustive): "
                            + json.dumps(issues)[:200])
            # The price lines MUST go in long_term_memory (browser-use drops
            # extracted_content from older steps; only long_term_memory persists
            # across the loop) — else the model "forgets" the prices and re-calls.
            mem = (f"shopping_search '{query}' -> {data.get('count', 0)} listings, cheapest first:\n"
                   + ("\n".join(lines[:8]) or "(none)"))
            return ActionResult(extracted_content=summary, long_term_memory=mem)
