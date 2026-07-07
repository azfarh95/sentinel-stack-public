"""Track B — P2: Dove-coexistence measurement.

Question: on the single -np 1 slot, if a browser task is mid-run, does a concurrent
'Dove turn' (a /v1/chat/completions call) get STARVED, or does it just queue behind
one browser generation via the broker FIFO and complete?

Method:
  1. baseline: 3 solo Dove-style chat calls (no browser load) -> uncontended latency.
  2. contended: launch a ~120s multi-step browser task; while it runs, fire Dove-style
     chat calls every ~10s; record each call's latency + HTTP status (200 vs fast-503).
  3. compare: added latency under contention should be bounded by ~one browser gen
     (the P0/P1b drain figure ~10-15s), NOT unbounded starvation; browser task still ok.

Read-only sandbox target. Health-gated at start so we don't pile onto a live Dove turn.
"""
import asyncio
import json
import time
import urllib.error
import urllib.request

from agent_runner import run_task, BRIDGE
from p1_suite import gate, health

DOVE_PROMPT = "You are a quick assistant. Reply with ONLY the numeric answer to: {a} + {b} = ?"


def chat_call(i: int, timeout: float = 150.0) -> dict:
    """One synchronous Dove-style chat completion through :8095 (the broker FIFO)."""
    body = json.dumps({
        "model": "qwen/qwen3.6-27b",
        "messages": [{"role": "user", "content": DOVE_PROMPT.format(a=i, b=i)}],
        "max_tokens": 24, "temperature": 0.0,
    }).encode()
    req = urllib.request.Request(BRIDGE + "/v1/chat/completions", data=body,
                                 headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        txt = (d["choices"][0]["message"]["content"] or "").strip()[:40]
        return {"i": i, "lat": round(time.time() - t0, 1), "status": r.status, "txt": txt, "err": None}
    except urllib.error.HTTPError as e:
        return {"i": i, "lat": round(time.time() - t0, 1), "status": e.code, "txt": None,
                "retry_after": e.headers.get("Retry-After"), "err": e.read().decode()[:100]}
    except Exception as e:
        return {"i": i, "lat": round(time.time() - t0, 1), "status": None, "txt": None,
                "err": f"{type(e).__name__}: {str(e)[:100]}"}


async def acall(i):
    return await asyncio.get_event_loop().run_in_executor(None, chat_call, i)


async def main():
    h = gate()
    print(f"gate: backend={h.get('backend')} active={h.get('active')}")

    print("\n--- BASELINE (solo Dove-style calls, no browser) ---")
    base = []
    for i in range(3):
        r = await acall(100 + i)
        base.append(r)
        print(f"  baseline[{i}] lat={r['lat']}s status={r['status']} txt={r['txt']!r}")
        await asyncio.sleep(2)
    base_lats = [r["lat"] for r in base if r["status"] == 200]
    base_med = sorted(base_lats)[len(base_lats) // 2] if base_lats else None
    print(f"  baseline median (200s only): {base_med}s")

    print("\n--- CONTENDED (Dove calls DURING a multi-step browser task) ---")
    btask = asyncio.create_task(run_task(
        "On https://quotes.toscrape.com use the 'Next' button to visit the first THREE pages; "
        "on each page report the FIRST quote's author. Label them page1/page2/page3.",
        label="p2-browser-load", max_steps=12, max_wall_s=200, use_vision=False))
    dove = []
    i = 0
    while not btask.done() and i < 8:
        await asyncio.sleep(10)
        if btask.done():
            break
        r = await acall(200 + i)
        dove.append(r)
        extra = f" retry_after={r.get('retry_after')}" if r.get("retry_after") else ""
        delta = (f" (+{round(r['lat'] - base_med, 1)}s vs base)"
                 if base_med and r["status"] == 200 else "")
        print(f"  dove[{i}] lat={r['lat']}s status={r['status']}{extra}{delta} txt={r['txt']!r}")
        i += 1
    bres = await btask
    print(f"  browser task: status={bres['status']} steps={bres['steps']} dur={bres['dur_s']}s "
          f"(solo ceil1-style was ~120s)")

    ok = [r for r in dove if r["status"] == 200]
    s503 = [r for r in dove if r["status"] == 503]
    fail = [r for r in dove if r["status"] not in (200, 503)]
    print("\n========== P2 SUMMARY ==========")
    print(f"baseline median: {base_med}s")
    if ok:
        lats = [r["lat"] for r in ok]
        print(f"contended Dove calls 200: n={len(ok)} min={min(lats)}s max={max(lats)}s "
              f"mean={round(sum(lats)/len(lats),1)}s")
        if base_med:
            print(f"  worst added latency: +{round(max(lats)-base_med,1)}s (bound ~= one browser gen)")
    print(f"fast-503 (backend loading): n={len(s503)}")
    print(f"hard failures: n={len(fail)} -> {[ (r['status'], r['err']) for r in fail ]}")
    print(f"browser task under contention: {bres['status']} in {bres['dur_s']}s")
    starved = bool(fail) or (ok and base_med and max(r["lat"] for r in ok) > base_med + 60)
    print(f"VERDICT: {'STARVATION RISK' if starved else 'COEXISTENCE OK (queued, bounded, no starvation)'}")


if __name__ == "__main__":
    asyncio.run(main())
