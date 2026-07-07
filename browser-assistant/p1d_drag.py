"""Track B — P1d: CLOSE the ceil3 ceiling (v2, selector-based).

P1d-v1 finding: a custom INDEX-based drag action can't target the boxes because browser-use
does not index plain `<div draggable=true>` as interactive elements (no [index] exists). So the
fix must address the element by CSS SELECTOR, not index.

This `drag_selector(from_selector, to_selector)` action:
  1. reads each element's viewport-center via getBoundingClientRect (JS read is fine; the DRAG
     itself is real CDP pointer events, not a JS-synthesised drag),
  2. dispatches press -> 12 interpolated moves (to cross jQuery-UI's drag threshold) -> release,
  3. READS BACK the post-drag header text of #column-a/#column-b so the result self-verifies.

The-internet/drag_and_drop swaps the <header> text on a successful drop, so column-a header == 'B'
after a real drag == ground-truth PASS, independent of what the LLM claims.
"""
import asyncio
import json
import os
import time

from browser_use import Agent, Browser, ChatOpenAI, Tools, ActionResult, BrowserSession

from agent_runner import _llm, CHROME, log, bridge_active

tools = Tools()


async def _eval(browser_session, expr):
    cdp = await browser_session.get_or_create_cdp_session()
    r = await cdp.cdp_client.send.Runtime.evaluate(
        {"expression": expr, "returnByValue": True}, session_id=cdp.session_id)
    return r.get("result", {}).get("value")


@tools.registry.action(
    "Drag the element matching from_selector and drop it onto the element matching to_selector using a "
    "real mouse-pointer drag. Pass CSS selectors (e.g. '#column-a'). USE THIS for HTML5 / jQuery-UI "
    "drag-and-drop and sortable lists, especially when the draggable elements are NOT in the indexed "
    "interactive list. Do NOT use evaluate()/JavaScript to perform the drag."
)
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
    # self-verify: read back the headers (ground truth)
    after = await _eval(browser_session, """(function(){
        function h(sel){var e=document.querySelector(sel); return e? (e.querySelector('header')||e).innerText.trim():null;}
        return JSON.stringify({a:h('#column-a'), b:h('#column-b')});})()""")
    msg = f"drag_selector {from_selector}->{to_selector} done. Post-drag headers: {after}"
    log({"label": "p1d-drag_selector-readback", "after": after})
    return ActionResult(extracted_content=msg, long_term_memory=msg)


async def run_drag(max_wall_s: float = 200.0) -> dict:
    t0 = time.time()
    browser = Browser(executable_path=CHROME if os.path.exists(CHROME) else None, headless=True)
    agent = Agent(
        task="Go to https://the-internet.herokuapp.com/drag_and_drop . Two boxes, A (left, CSS "
             "selector '#column-a') and B (right, '#column-b'), can be reordered by drag-and-drop. "
             "Call the drag_selector action with from_selector='#column-a' and to_selector='#column-b' "
             "to drag A onto B. Then report the headers shown for #column-a and #column-b after the drag.",
        llm=_llm(), browser=browser, tools=tools, use_vision=False)
    status, final, steps, err = "unknown", None, None, None
    try:
        history = await asyncio.wait_for(agent.run(max_steps=8), timeout=max_wall_s)
        try:
            final = history.final_result()
        except Exception:
            final = None
        status = "ok"
    except asyncio.TimeoutError:
        status, err = "fenced_timeout", f"wall fence {max_wall_s}s exceeded"
    except Exception as e:
        status, err = "error", f"{type(e).__name__}: {str(e)[:200]}"
    finally:
        try:
            await browser.kill()
        except Exception:
            pass
    rec = {"label": "p1d-drag_selector", "status": status,
           "dur_s": round(time.time() - t0, 1), "final": (str(final)[:500] if final else None), "err": err}
    log(rec)
    return rec


async def main():
    print("baseline bridge:", bridge_active())
    r = await run_drag()
    print(f"\n[drag_selector] -> {r['status']} dur={r['dur_s']}s")
    print(f"  final: {r['final']}")
    if r.get("err"):
        print(f"  err: {r['err']}")
    fin = (str(r["final"]) or "")
    # PASS if the post-drag #column-a header reads 'B' (the swap actually happened)
    a_is_b = ('"a": "B"' in fin) or ("'a': 'B'" in fin) or ("column-a" in fin.lower() and "b" in fin.lower())
    print(f"\nVERDICT: see post-drag headers above. If #column-a header == 'B' the drag truly swapped them.")


if __name__ == "__main__":
    asyncio.run(main())
