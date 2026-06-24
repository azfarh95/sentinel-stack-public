# Track B — P0/P1 findings + failure taxonomy (2026-06-16, autonomous run)

Harness: `agent_runner.py` (wall-fenced browser-use) + `p1_suite.py` + `p1b_ceiling.py`.
LLM = local Qwen 3.6 27B via infer-bridge `:8095` (broker-gated, 2.3 fast-503).
Browser = throwaway **headless** Chrome (separate profile) — never touched Comet / any real account.
All targets purpose-built sandboxes (quotes.toscrape.com, the-internet.herokuapp.com). Raw log: `runs.jsonl`.

## P0 — the fence (turn-fence analog) — PASS
- Normal task succeeds; a stuck multi-step task hits the **wall-clock fence** (`asyncio.wait_for`)
  and the agent loop is cancelled → `fenced_timeout`.
- **Slot drain**: after a fence fires, `:8095 active` returns to `0` within **~10–15 s** — NOT a
  permanent wedge. The residue is **one trailing 27B generation** completing on the `-np 1` slot
  (llama.cpp finishes the in-flight gen even though the HTTP client disconnected). So worst-case a
  fenced browser task delays a concurrent Dove turn by *one generation* (~10–15 s), then frees.

## P1 — capability suite — 5/5 PASS (all DOM-index, no grounding model)
| test | result | steps | dur | note |
|---|---|---|---|---|
| T1 read/extract | ok | 2 | 37s | 3 quotes+authors verbatim |
| **T2 form fill+submit** | ok | 3 | 43s | typed user/pass into correct DOM indices, clicked Login, read back the flash msg. **De-risks tier-2 (memory had it uncertain).** |
| T3 multi-tab | ok | 3 | 48s | opened 2nd tab, read both |
| V vision A/B | ok | 2 | 33s vs 41s | **vision = +8s (~24%) slower, zero accuracy gain on a DOM-readable page** |

## P1b — ceiling probe — found the exact break point
| test | result | steps | dur | note |
|---|---|---|---|---|
| ceil1 paginate+aggregate | ok | 6 | 120s | 3 pages, per-page count accumulated in memory (3+1+2=6) |
| ceil2 click-to-filter | ok | 3 | 59s | clicked `truth` tag, read all 4 filtered quotes+authors |
| **ceil3 HTML5 drag-and-drop** | **fenced_timeout** | — | 260s | **THE CEILING** |

ceil3 detail: agent tried ~9 distinct JS strategies (HTML5 DnD events, jQuery-UI API, synthetic
mouse/pointer sequences, DataTransfer, click-to-select). browser-use's **own loop-detection nudge
fired (stagnation=5) and could not break it.** The **external wall-fence** is what stopped it at 260s.
After the fenced cancel (a *vision* task, 9 steps of flailing) the **slot still drained to `active=0`
and the backend stayed `ready`** → the Dove-protection fence is validated against a *real* wedge.

## P1c — broader web ceiling (the patterns REAL sites use) — 6/6 PASS
| test | result | steps | dur | note |
|---|---|---|---|---|
| jsrender (JS-injected DOM) | ok | 2 | 40s | waits for JS render; read correctly |
| infinite-scroll | ok | 5 | 81s | scrolled to bottom, used `find_elements(div.quote)` → exact count **100** |
| table extract+reason | ok | 2 | 47s | largest Due $100 → jdoe@hotmail.com (numeric reasoning) |
| native `<select>` dropdown | ok | 3 | 38s | used the `select_dropdown` action |
| login + session | ok | 3 | 45s | logged in, confirmed Logout link present |
| **iframe (TinyMCE editor)** | ok | 6 | 94s | **soft ceiling** — see taxonomy #5 |

iframe detail: agent dismissed a blocking alert, recognised the editor is in a nested context, tried
DOM-index click/type (couldn't reach the editable body), then **self-corrected to a JS-eval bridge**
into `iframe.contentDocument.body` and set the text. Works, but ~2× the steps and needs the JS fallback.

(Harness note: the P1c suite first crashed on a **cp1252 `print()`** of a `✅` before the iframe test —
a Windows-console encoding bug on my side, not a browser/model failure; `runs.jsonl` was utf-8 and intact.
Fixed in `agent_runner.py` by forcing utf-8 stdout. iframe then re-run standalone and passed.)

## Reliability ceiling map (local 27B + DOM-index, no grounding model)
- ✅ **Reliable (14 capabilities validated):** read/extract · form fill+submit · multi-tab · multi-page
  nav+aggregation · click-to-filter · JS-rendered DOM · infinite-scroll+count · table extract+reason ·
  native `<select>` · login+session. (Tiers 1–3 + light tier-4 reasoning — clean, 2–6 steps, no flailing.)
- 🟡 **Works with friction (soft ceiling):** **iframe / nested context** — needs a JS-eval bridge, ~2× steps.
- ✅ **HTML5 / jQuery-UI drag-and-drop — CEILING CLOSED (P1d, verified):** the `drag_selector` custom
  action (CDP pointer drag + interpolated moves) swapped the boxes in 1 step (taxonomy #1). The default
  config can't (it JS-evals; the divs aren't indexed) — the ~30-LOC action is the fix.
- ❌ **Still out of reach without a grounding model:** the truly **DOM-blind / canvas / image-of-text**
  residue (no selector, no index, target only visible in pixels). This is the *only* place the research
  report's grounding lane (UI-TARS-1.5-7B-GGUF in llama-swap) actually earns its keep — defer until a
  real task needs it.

## Failure taxonomy (what actually goes wrong + the fix)
1. **Gesture/DnD (HTML5/jQuery drag-and-drop)** — was the one hard ceiling; **now CLOSED + VERIFIED
   in P1d** with a ~30-LOC custom action. Two real blockers, found by iterating:
   - **(a)** browser-use's default guidance routes drag through **JS-eval** (the `evaluate` docstring
     says *"use for hover, drag, zoom…"*) and synthetic JS events don't drive native DnD → ceil3's 9
     failed JS attempts.
   - **(b)** browser-use **does not index plain `<div draggable=true>` as interactive elements**, so an
     INDEX-based custom action has nothing to target (P1d-v1: agent confirmed the divs exist but no
     `[index]` was ever assigned → couldn't call the action).
   - **The fix that WORKED (P1d-v2, VERIFIED):** a **selector-based** action — resolve each CSS selector's
     viewport center via `getBoundingClientRect`, then dispatch a **real CDP pointer drag** with
     **interpolated moves** (press → 12 small `mouseMoved` steps to cross jQuery-UI's drag threshold →
     release). One `drag_selector('#column-a','#column-b')` call swapped the boxes; the action's own
     **DOM readback confirmed ground truth** (`#column-a` header → 'B', `#column-b` → 'A'). 1 action, 52s
     — vs the 260s flail+fence in P1b. Code: `p1d_drag.py`. Key lessons: **address by SELECTOR not index**
     for non-indexed elements, and **interpolate the moves** (a single jump / browser-use's built-in
     `element.drag_to` would not fire jQuery-UI).
   - A **grounding model** (research report's UI-TARS-1.5-7B) remains only for the *truly* index-less &
     selector-less residue (canvas / image-of-text). Ordinary web drag-drop does **not** need it.
2. **Stagnation loops** — browser-use's built-in loop nudge is necessary but **not sufficient**; the
   **wall-fence is the real backstop** (validated by ceil3). Keep the fence mandatory.
3. **Page-readiness 8s timeout warnings** — appear on JS-heavy pages (quotes.toscrape, herokuapp);
   browser-use warns then proceeds fine. Benign so far; watch if it ever aborts a real task.
5. **iframe / nested browsing context (SOFT ceiling)** — reachable but with friction: DOM-index
   can click the iframe element yet can't type into the inner editable body, so the agent must
   JS-bridge into `contentDocument` (it figured this out unaided in 6 steps). *Optional polish:* a
   custom `type_into_iframe(iframe_index, text)` action (same pattern as the drag fix) would make it
   1-step + reliable. Lower priority than drag — the JS fallback already works.
4. **Vision is a latency tax with no web benefit** — +~24% per step, no accuracy gain on DOM-readable
   pages. Empirical backing for **Config C**: DOM-index primary; vision/grounding reserved for the
   DOM-blind residue (canvas, image-of-text, obfuscated DOM, gestures).

## P2 — Dove coexistence (real contention) — COEXISTENCE OK
Method: 3 baseline solo chat calls, then 3 'Dove turn' chat calls fired DURING a live 4-step browser task,
all through `:8095`.
- baseline median **0.4s**; contended Dove calls **all 200, correct answers**, lat 5.4–14.2s.
- **worst added latency +13.8s = exactly one browser generation** (matches the P0/P1b drain figure).
- **0 hard failures, 0 fast-503**; browser task still completed ok (66s) under the chat load.
- **Verdict: queued, bounded, no starvation.** The broker FIFO + `-np 1` serialize a browser step and a
  Dove turn cleanly — a Dove turn landing mid-browser-step waits ≤ one generation, then completes.
- **Caveat (UX, not safety):** +14s is noticeable in chat. A **Dove-priority gate** (browser defers its
  next step while a Dove turn is in flight) would erase the tax — a P2.5 refinement, not a blocker. Other
  levers: cap browser concurrency, shorter `max_tokens` per browser step, keep-warm (already on).

## Implication for the build order
- Tiers 1–3 are **production-viable on local 27B today** via DOM-index — no grounding model needed yet.
- The grounding/gesture lane (research report's UI-TARS-1.5-7B-GGUF in llama-swap, or native CDP drag)
  is the **fallback for the DOM-blind residue** — defer until a real task needs it.
- The fence is **non-negotiable** for Dove coexistence and is now validated against a genuine wedge.
