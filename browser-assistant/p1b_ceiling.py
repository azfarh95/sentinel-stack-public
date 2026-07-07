"""Track B — P1b: CEILING probe. Push local-27B + DOM-index until it breaks, to
map the reliable ceiling + build the failure taxonomy (memory flagged tier-4 flaky).

All targets are purpose-built sandboxes; no real-account / no server state change:
  ceil1  multi-page nav + aggregation   quotes.toscrape.com (paginate + count/filter)
  ceil2  click-to-filter + extract       quotes.toscrape.com /tag/ (interactive filter)
  ceil3  HTML5 drag-and-drop             the-internet.herokuapp.com/drag_and_drop
         (the classic DOM-index BREAKER: native HTML5 DnD that simple clicks can't do —
          expected to struggle/fail → a taxonomy data point, not a regression)

Health-gated + fenced (reuses P0/P1 machinery). Appends to runs.jsonl.
"""
import asyncio
import time

from agent_runner import run_task, log
from p1_suite import gate


async def main():
    t0 = time.time()
    results = []
    tests = [
        dict(label="ceil1-paginate-agg", max_steps=16, max_wall_s=320, use_vision=False,
             task="On https://quotes.toscrape.com , use the 'Next' button to go through the "
                  "first THREE pages. Count how many quotes in total (across those 3 pages) are "
                  "attributed to Albert Einstein, and list each such quote's first few words. "
                  "Report the final count."),
        dict(label="ceil2-click-filter", max_steps=12, max_wall_s=260, use_vision=False,
             task="On https://quotes.toscrape.com , click the tag 'truth' in the 'Top Ten tags' "
                  "sidebar box (this filters quotes by that tag). Then report how many quotes are "
                  "shown for that tag and the author of each."),
        dict(label="ceil3-html5-dnd", max_steps=14, max_wall_s=260, use_vision=True,
             task="Go to https://the-internet.herokuapp.com/drag_and_drop . There are two boxes, "
                  "'A' (left) and 'B' (right). Drag box A onto box B so their positions swap. "
                  "Then report the new left-to-right order of the box labels."),
    ]
    for t in tests:
        h = gate()
        gated = h.get("ok") and h.get("backend") == "ready" and not h.get("active")
        print(f"\n=== {t['label']} === gate backend={h.get('backend')} active={h.get('active')} run={gated}")
        if not gated:
            rec = {"label": t["label"], "status": "skipped_gate", "health": h}
            log(rec); results.append(rec); print(f"  SKIPPED — {h}"); continue
        r = await run_task(t["task"], label=t["label"], max_steps=t["max_steps"],
                           max_wall_s=t["max_wall_s"], use_vision=t["use_vision"])
        results.append(r)
        print(f"  -> {r['status']} | steps={r['steps']} dur={r['dur_s']}s vision={r['use_vision']}")
        print(f"     final: {str(r['final'])[:300]}")
        if r.get("err"):
            print(f"     err: {r['err']}")
        await asyncio.sleep(6)

    print("\n========== P1b CEILING SUMMARY ==========")
    for r in results:
        print(f"{r['label']:20} {r.get('status'):16} steps={r.get('steps')} dur={r.get('dur_s')}s")
    print(f"suite wall: {round(time.time() - t0, 1)}s")
    log({"label": "p1b-ceiling-done",
         "statuses": {r["label"]: r.get("status") for r in results},
         "suite_wall_s": round(time.time() - t0, 1)})


if __name__ == "__main__":
    asyncio.run(main())
