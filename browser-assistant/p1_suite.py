"""Track B — P1: capability validation suite (runs through the P0 fence).

Tiers (all on PURPOSE-BUILT scraping/test sandboxes — never a real account):
  T1  read/extract           quotes.toscrape.com  (structured DOM extract)
  T2  fill-form + submit      the-internet.herokuapp.com/login (designated test creds)
  T3  multi-tab               two tabs, report each
  V   vision A/B              same extract with use_vision False vs True (token/latency cost)

Each test is HEALTH-GATED: before it runs we confirm the bridge backend is
`ready` and the slot is free (active==False) so we never start a browser test
on top of a live Dove turn. Between tests we re-check; if Dove is mid-turn
(active==True) we wait briefly and only proceed once the slot frees.

Structured results append to runs.jsonl (shared with P0) + a P1 summary print.
"""
import asyncio
import json
import time
import urllib.request

from agent_runner import run_task, BRIDGE, log


def health() -> dict:
    try:
        with urllib.request.urlopen(BRIDGE + "/health", timeout=4) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "err": type(e).__name__}


def gate(max_wait_s: float = 90.0) -> dict:
    """Wait until backend ready AND slot free (Dove not mid-turn). Returns last health."""
    t0 = time.time()
    h = health()
    while time.time() - t0 < max_wait_s:
        if h.get("ok") and h.get("backend") == "ready" and not h.get("active"):
            return h
        time.sleep(3)
        h = health()
    return h  # proceed anyway after the wait; caller logs the state


async def main():
    suite_t0 = time.time()
    results = []

    tests = [
        dict(label="t1-extract", max_steps=8, max_wall_s=150, use_vision=False,
             task="Go to https://quotes.toscrape.com and report the first THREE quotes on the "
                  "page, each with its author. Format as a numbered list."),
        dict(label="t2-form-login", max_steps=10, max_wall_s=180, use_vision=False,
             task="Go to https://the-internet.herokuapp.com/login . This is a test sandbox. "
                  "Log in using username 'tomsmith' and password 'SuperSecretPassword!' "
                  "(these are the published demo credentials shown on the page). After logging "
                  "in, report the exact green success/flash message text shown at the top."),
        dict(label="t3-multitab", max_steps=10, max_wall_s=180, use_vision=False,
             task="Open https://quotes.toscrape.com in the current tab and report its page title. "
                  "Then open https://example.com in a NEW tab and report ITS <h1>. "
                  "Report both results clearly labelled tab1 and tab2."),
        dict(label="v-novision", max_steps=8, max_wall_s=150, use_vision=False,
             task="Go to https://quotes.toscrape.com and report every tag listed in the "
                  "'Top Ten tags' box in the right sidebar."),
        dict(label="v-vision", max_steps=8, max_wall_s=200, use_vision=True,
             task="Go to https://quotes.toscrape.com and report every tag listed in the "
                  "'Top Ten tags' box in the right sidebar."),
    ]

    for t in tests:
        h = gate()
        gated = h.get("ok") and h.get("backend") == "ready" and not h.get("active")
        print(f"\n=== {t['label']} === gate: backend={h.get('backend')} active={h.get('active')} ok_to_run={gated}")
        if not gated:
            # Dove may be mid-turn or backend not ready — record a skip rather than pile on.
            rec = {"label": t["label"], "status": "skipped_gate", "health": h}
            log(rec); results.append(rec)
            print(f"  SKIPPED (gate not clear) — {h}")
            continue
        r = await run_task(t["task"], label=t["label"], max_steps=t["max_steps"],
                           max_wall_s=t["max_wall_s"], use_vision=t["use_vision"])
        results.append(r)
        print(f"  -> {r['status']} | steps={r['steps']} dur={r['dur_s']}s "
              f"vision={r['use_vision']}\n     final: {str(r['final'])[:220]}")
        if r.get("err"):
            print(f"     err: {r['err']}")
        # let the trailing gen drain before the next test (slot hygiene)
        await asyncio.sleep(6)

    print("\n========== P1 SUMMARY ==========")
    for r in results:
        print(f"{r['label']:16} {r.get('status'):16} steps={r.get('steps')} "
              f"dur={r.get('dur_s')}s vision={r.get('use_vision')}")
    print(f"suite wall: {round(time.time() - suite_t0, 1)}s")
    log({"label": "p1-suite-done", "n": len(results),
         "statuses": {r["label"]: r.get("status") for r in results},
         "suite_wall_s": round(time.time() - suite_t0, 1)})


if __name__ == "__main__":
    asyncio.run(main())
