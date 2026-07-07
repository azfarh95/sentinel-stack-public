"""Track B — P1c: broader web ceiling. Beyond clean static DOM — the patterns REAL
sites use: JS-rendered content, infinite scroll, data tables, native <select>, iframes,
login+session. Maps where DOM-index reliability degrades on realistic web.

All targets purpose-built sandboxes (no real account, read-only / dummy creds):
  jsrender   quotes.toscrape.com/js/      content injected by JS (does browser-use wait for render?)
  scroll     quotes.toscrape.com/scroll   infinite scroll (load-on-scroll accumulation)
  table      the-internet.herokuapp.com/tables    table extract + numeric reasoning
  dropdown   the-internet.herokuapp.com/dropdown  native <select>
  iframe     the-internet.herokuapp.com/iframe    TinyMCE editor INSIDE an iframe (known-hard)
  session    quotes.toscrape.com/login    dummy login (accepts anything) + session/logout check
"""
import asyncio
import time

from agent_runner import run_task, log
from p1_suite import gate


async def main():
    t0 = time.time()
    results = []
    tests = [
        dict(label="p1c-jsrender", max_steps=8, max_wall_s=170, use_vision=False,
             task="Go to https://quotes.toscrape.com/js/ . The quotes on this page are rendered by "
                  "JavaScript. Report the first THREE quotes with their authors."),
        dict(label="p1c-scroll", max_steps=14, max_wall_s=240, use_vision=False,
             task="Go to https://quotes.toscrape.com/scroll . This page loads more quotes as you "
                  "scroll down. Scroll to the bottom repeatedly until no more quotes load, then "
                  "report the TOTAL number of quotes now on the page."),
        dict(label="p1c-table", max_steps=10, max_wall_s=200, use_vision=False,
             task="Go to https://the-internet.herokuapp.com/tables . In the first data table "
                  "(id 'table1'), find the person with the LARGEST 'Due' amount and report that "
                  "person's Email and the Due value."),
        dict(label="p1c-dropdown", max_steps=8, max_wall_s=160, use_vision=False,
             task="Go to https://the-internet.herokuapp.com/dropdown . Select 'Option 2' from the "
                  "dropdown menu, then confirm and report which option is currently selected."),
        dict(label="p1c-session", max_steps=10, max_wall_s=200, use_vision=False,
             task="Go to https://quotes.toscrape.com/login . This is a demo login that accepts any "
                  "credentials. Log in with username 'admin' and password 'admin'. Then confirm you "
                  "are logged in by checking that a 'Logout' link is now present, and report whether "
                  "it is."),
        dict(label="p1c-iframe", max_steps=12, max_wall_s=220, use_vision=False,
             task="Go to https://the-internet.herokuapp.com/iframe . There is a rich-text editor "
                  "embedded inside an IFRAME. Clear it and type the text 'hello sentinel' into the "
                  "editor, then report the editor's current text content."),
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
        print(f"  -> {r['status']} | steps={r['steps']} dur={r['dur_s']}s")
        print(f"     final: {str(r['final'])[:280]}")
        if r.get("err"):
            print(f"     err: {r['err']}")
        await asyncio.sleep(6)

    print("\n========== P1c WEB-CEILING SUMMARY ==========")
    for r in results:
        print(f"{r['label']:16} {r.get('status'):16} steps={r.get('steps')} dur={r.get('dur_s')}s")
    print(f"suite wall: {round(time.time() - t0, 1)}s")
    log({"label": "p1c-web-done",
         "statuses": {r["label"]: r.get("status") for r in results},
         "suite_wall_s": round(time.time() - t0, 1)})


if __name__ == "__main__":
    asyncio.run(main())
