# Track B (Browser Assistant) — autonomous session report, 2026-06-16

**Mandate:** "start Track B, hand over for ~4 hours, update every turn on Telegram."
**Window:** ~07:25 → ~08:50 UTC (the planned P0–P2 scope finished early; I extended into P1b/P1c/P1d).
**Boundaries honored:** throwaway **headless** Chrome only (never Comet / any real account); read-only +
dummy creds on **purpose-built sandboxes** only; **no live-stack restarts/deploys/config changes**; the
watchdog model-server supervisor stayed dry-run; Dove health-checked before every load + each test
health-gated (never started on top of a live Dove turn); integration phases (C/`:8098`/model-swaps) left
for you. Net effect on the box: **zero** — no live service touched.

---

## TL;DR (compact)

- **The whole planned scope (P0–P2) is validated, plus I went further.** Local Qwen-27B + browser-use is
  **production-viable for real web work today** via DOM-index — no grounding model needed.
- **14 capabilities reliable**, **1 soft ceiling** (iframe — works with a JS bridge), **1 hard ceiling
  (HTML5 drag-drop) which I then CLOSED + verified** with a ~30-LOC custom CDP drag action.
- **Browser ⇄ Dove coexistence is SAFE** on the single GPU slot: no starvation, worst-case **+14s** to a
  Dove turn that lands mid-browser-step (= one generation), 0 failures.
- **The fence works against a *real* runaway** (not just a synthetic timeout) — the key Dove-protection.
- Nothing is committed yet; all artifacts are additive files in `metamcp-local/browser-assistant/`.

---

## What I built + ran

A fenced test harness (the browser-side analog of the 3.3 turn-fence) and 5 capability suites, all routing
LLM calls through infer-bridge `:8095` (broker FIFO + 2.3 fast-503):

| file | purpose |
|---|---|
| `agent_runner.py` | fenced `run_task()` — wall-clock `asyncio.wait_for` cap + JSONL logging + utf-8 fix |
| `p1_suite.py` | tier-1/2/3 + vision A/B (health-gated) |
| `p1b_ceiling.py` | ceiling probe (paginate-agg, click-filter, drag-drop) |
| `p1c_web.py` + `p1c_iframe.py` | real-world web (JS-render, scroll, table, dropdown, session, iframe) |
| `p2_coexist.py` | Dove-coexistence contention measurement |
| `p1d_drag.py` | the custom `drag_selector` CDP-drag action that closes the drag ceiling |
| `FINDINGS-P1.md` | full taxonomy + build-order implications · `runs.jsonl` raw log |

---

## Validation matrix (verbose)

**Reliable on local 27B + DOM-index (2–6 steps, no flailing):**
| capability | result | note |
|---|---|---|
| read / extract | ✓ 2 steps 37s | verbatim quotes+authors |
| **form fill + submit** | ✓ 3 steps 43s | typed creds into right indices, clicked Login, read flash — **de-risks tier-2** |
| multi-tab | ✓ 3 steps 48s | opened 2nd tab, read both |
| multi-page nav + aggregation | ✓ 6 steps 120s | counted across 3 pages via memory |
| click-to-filter | ✓ 3 steps 59s | clicked tag, read all 4 results |
| JS-rendered DOM | ✓ 2 steps 40s | waits for JS render |
| infinite-scroll + count | ✓ 5 steps 81s | scrolled to bottom, `find_elements` → exact 100 |
| table extract + numeric reason | ✓ 2 steps 47s | largest Due $100 → correct email |
| native `<select>` | ✓ 3 steps 38s | used `select_dropdown` |
| login + session | ✓ 3 steps 45s | confirmed Logout link |

**Vision A/B:** `use_vision=True` was **+~24% latency for ZERO accuracy gain** on DOM-readable pages →
keep vision for DOM-blind only (validates Config C).

**Soft ceiling — iframe (TinyMCE):** ✓ but 6 steps — DOM-index couldn't type into the nested editor body,
so the agent **self-corrected** to a JS bridge into `contentDocument`. Works; a small custom
`type_into_iframe` action would make it 1-step.

**Hard ceiling — HTML5/jQuery drag-drop → CLOSED + VERIFIED:**
- In P1b it was the breaker: 260s, 9 failed JS-eval attempts, browser-use's own loop-detector couldn't
  break it — **the wall-fence stopped it** (and the slot still drained to `active=0` afterward → fence
  validated against a *real* wedge).
- Diagnosis (took 2 iterations): (a) default guidance routes drag to JS-eval, which can't drive native
  DnD; (b) browser-use **doesn't index plain `<div draggable>`**, so an index-based action has no target.
- **Fix that worked:** a **selector-based** `drag_selector('#column-a','#column-b')` — resolve each
  selector's center via `getBoundingClientRect`, then a **real CDP pointer drag with 12 interpolated
  moves** (to cross jQuery-UI's threshold). The action **reads back the DOM** to self-verify: post-drag
  `#column-a`='B', `#column-b`='A' → genuine swap. **1 step, 52s.**

---

## Dove coexistence (P2) — the number you care about

On the single `-np 1` slot, with a browser task running, I fired Dove-style chat calls concurrently:
- baseline (uncontended) Dove turn: **0.4s**;
- contended: **all 200, correct answers**, **worst +13.8s = exactly one browser generation**;
- **0 hard failures, 0 fast-503**, browser task still completed.

**Verdict: safe, bounded, no starvation.** The broker FIFO serializes a browser step and a Dove turn —
a Dove turn landing mid-step waits ≤ one generation. *Caveat (UX, not safety):* +14s is noticeable in
chat. The clean fix is a **broker priority lane** (Dove preempts a browser step) — a live-stack change, so
left for you; cheaper hedges = shorter browser `max_tokens` or keep-warm (already on).

---

## What's NOT done (needs you — out of autonomous scope)

1. **Productionise the harness** — fold `drag_selector` (+ optional `type_into_iframe`) into a reusable
   tool-set; add retry-once-on-transient; keep the fence mandatory.
2. **C prerequisite (surface-unification)** — gate web surfaces on `:8098` auth + retire unauth
   `comet-sidepanel :8101` (closes the S1 account-takeover). *Auth refactor — I did not touch it.*
3. **Integrate B as the `:8098` browser-mode** — CDP-attach to Comet, tool-scope, approval-gate.
4. **Broker priority lane** for Dove (erases the +14s).
5. **Commit** — all session artifacts are uncommitted, additive files under `browser-assistant/`
   (sentinel-stack repo). `.gitignore` updated to skip transient logs. I left the commit to you; say the
   word and I'll commit them on a branch.

A grounding model (research report's UI-TARS-1.5-7B-GGUF) is now needed **only** for the truly
DOM-blind/canvas residue — defer until a real task hits it.
