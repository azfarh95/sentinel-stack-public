# A-Resolution — make Dove reliable for real daily use (research-backed)

**Date:** 2026-06-15 · **Goal (A):** the personal assistant (security-duty roster →
reminders → calendar → notes) completes turns reliably, no wedges/aborts. NOT the
browser assistant (B) or surface unification (C) — those are separate, deferred.
**Basis:** 5-stream forensic + external research (this session). Evidence-cited; two of
my own prior conclusions are corrected here.

---

## 0. Two corrections to the prior narrative (lead with the uncomfortable)

1. **~80% of "broken all day" was self-inflicted churn, not persistent poison.** The
   watchdog audit: 114/158 state-transitions today were boot artifacts from *2 daemon
   redeploys*; the 12:05 "10-services-down" was *one* Docker-engine blip (auto-suppressed,
   self-recovered 60–80s); the 09:33 broker block/evict was a *manual live test*. The
   genuine intervention-independent poison is **~20%, and it's one service: openclaw**.
2. **The KV-quant regression is NOT the cause — GPU contention is.** The research agents
   blamed q8_0/f16 KV (they read stale memory/docs). Today's clean A/B proved KV quant is
   speed-neutral; the slowness is **contention** (idle ComfyUI → Qwen KV spill). Proof:
   a clean fast window **08:32–10:46 UTC at 9 s/step** right after a manual ComfyUI free,
   which then **regressed** — contention coming/going, not a constant KV property.
   **Action item: scrub the KV-regression framing from docs/memory — it is actively
   misleading future analysis (it misled the research agents).**

---

## 1. The poison is a COMPOUND (not one root cause)

| # | Cause | Evidence | Fix lever |
|---|---|---|---|
| P1 | **Slow first-token = GPU contention** (idle ComfyUI spills Qwen KV → ~40 vs ~736 tok/s) | uniform 50–250 s/step all day; fast only when card clean (Agent 1); evening partial-spill 18.48/22 GB | one-chair broker (done) **+ keep-Qwen-warm (new, §3.1)** |
| P2 | **`-np 1` single-slot cascade** | the 07:11 wolfies cron **wedged the only slot 3 h** (07:11→10:12 dead zone) → all queued (Agent 3) | turn-level fence/reaper (§3.3) + `/stop` |
| P3 | **OpenClaw self-abort is unwired** (stuck detected, never aborted) | docs claim `stuckSessionAbortMs`; issues show it doesn't exist/fire (#71127, #17258, #11520); the watchdog "restart" is a **false-success no-op**, gateway self-heals on a ~10-min timer (Agent 2/5) | external turn-level fence (§3.3); don't trust in-product recovery |
| P4 | **Error-WRITE poison (R1 fix was INCOMPLETE)** | 5 new error rows today; **newest DB row (11:50) is a fresh `[bridge_error]`**; I only stopped the *replay*, not the *write* (Agent 4) | write-side filter (§3.2) |
| P5 | **eventbus Postgres-auth drift** | `password authentication failed for metamcp_user` ×24 on reconnect (Agent 3) | fix the DSN/env-load (§3.4) |
| P6 | **R2 orphans, no reaper** | 11 `streaming_done=false` rows, oldest 20 d (Agent 4) | startup+periodic reaper + read-filter (§3.5) |

**Failure rate today: ~40% of turns (12/30).** Every failure traces to P1×P2×P3 (slow →
wedge → no self-abort → timeout fence), with P4 silently re-poisoning the store.

## 2. What the research VALIDATED (structural, but not needed for A)

OpenClaw **cannot be made reliable by config alone** — self-abort (#71127), cold-start
failover (#43946, #4992 404-hangs-forever), and fallback-recovery (#47705 sticky
overwrite, #43400 never-fires-on-timeout) are unwired or "closed as not planned"; docs
over-promise knobs that don't fire (Agent 5). The community's real fix = **structural**:
- **Route turns through the persistent gateway** — and `openclaw agent` **already routes
  to the running gateway by default** (embedded one-shot only on failure / `--local`).
  Gateway submit API exists (WS `agent`/`sessions.send`; HTTP `/v1/chat/completions`).
- **Single model, no fallback** — **we're already immune** (`fallbacks:[]` empty; #47705
  spares no-fallback agents).
- **Keep an external fence** — ours (watchdog restart) is the no-op; a **turn-level
  Python timeout that force-finalizes** is the real fence.

**Fork resolved:** A needs **patch + verify-gateway-routing**, NOT the full own-the-loop
rewrite. Own-the-loop (Phase-5 Option B) is only for the browser future (B). Doing the
rewrite for A would be over-engineering.

## 3. The fix plan (ordered by leverage; all our code/config)

### 3.1 Keep Qwen warm — the highest-leverage NEW insight
The TTL idle-unload (1800 s) is a major contributor: it causes (a) **cron cold-load
aborts** (the 180–250 s zero-activity stalls = cold-load on an unloaded model) and (b)
**reload-under-residual-contention** (the evening 18.48 GB partial spill — Qwen reloaded
via TTL while ComfyUI held ~2 GB, so KV only partially fit). The one-chair fix is
event-driven on *lease-release* and does NOT cover a TTL-reload that has no lease cycle.
**Fix:** keep Qwen resident by default — raise/disable the llama-swap `ttl` so it
idle-unloads only when the broker explicitly evicts it for a FLUX/gaming lease (which it
already does), and the broker reloads/keeps-warm after. Kills P1's reload path AND the
cron cold-loads in one move. (Supersedes the weaker "warm-gate R7".)

### 3.2 Stop the error-WRITE poison (completes R1)
`brain_wrapper.chat_turn_finish` finalizes the reserved assistant row with the error text
when `reply.ok` is false → the store keeps accumulating `[bridge_error]`/`status=None`/
`[interrupted]` rows (Agent 4: still happening today). The read-side predicate (shipped)
only suppresses *replay*. **Fix:** on a failed turn, do NOT persist the error as
replayable assistant content — finalize the row as failed/empty + a non-replayable flag
(or skip the row), so the store can't self-poison even before the loader filters it.

### 3.3 Turn-level fence + reaper (P2/P3)
A stuck turn must not hold the only slot for hours. **Fix:** a `concurrent.futures`
turn-level Python timeout in the dispatcher/bridge that force-finalizes the reserved row
(emit `message.complete`) instead of trusting OpenClaw to self-abort; + a startup +
periodic reaper that finalizes orphaned `streaming_done=false` rows as `[interrupted]`.

### 3.4 Fix the eventbus Postgres-auth drift (P5)
The `metamcp_user` password-auth failures on reconnect = the DSN/env-load issue (likely
the same `.env`/WCM password the bot/mirror read). **Fix:** ensure `brain_store`/eventbus
build the DSN from the *live* password (add `_load_env_local()` to `brain_store` import so
bot/mirror/scheduler all get it — O4), and verify raw-WCM == .env.local == live-DB.

### 3.5 Reaper + read-filter (P6) — fold into 3.3.

### 3.6 Verify gateway-routing (the cheap structural check)
Confirm `brain_wrapper.openclaw_one_shot`'s `node openclaw agent … --json --timeout 600`
**routes through the persistent gateway** (not embedded / not `--local`). If embedded,
switching to gateway-routing removes the spawn-cost + the session-takeover race (#84460)
— possibly a one-line change. If it already routes, the spawn is a light client and the
slowness is purely the model (so the spawn-rewrite isn't even needed for A).

## 4. Verification matrix
- **P1:** with Qwen kept warm + card clean, a cron + a multi-step turn run at <10 s/step
  (the proven fast-window rate), not 50–250 s.
- **P4:** after a deliberately-failed turn, NO new error row is persisted as replayable
  assistant content; `load_for_llm` preamble carries none.
- **P2/P3:** a deliberately-wedged turn force-finalizes at the Python fence (~the budget),
  freeing the slot — it does NOT block the next turn for minutes.
- **P5:** eventbus reconnects with zero `password authentication failed` over an hour.
- **3.6:** a turn is observed hitting the gateway (warm), not cold-spawning embedded.

## 5. Out of scope for A
Model quality (Qwen-27B ceiling) and `-np 1` throughput (concurrency) — AI-005, separate.
The own-the-loop rewrite, browser assistant, surface unification — B/C, deferred.

## 6. Insights surfaced by writing this up
- **Keep-warm (3.1) is bigger than the warm-gate I'd scoped** — it closes both the
  cron-cold-load aborts AND the reload-under-contention gap the one-chair fix can't reach.
- **My R1 fix was half-done** (read-side only); the write-side (3.2) is the actual
  poison-stop.
- **My own stale docs/memory are now a *liability*** — they misled the research agents
  into the debunked KV theory. Doc hygiene is a reliability concern, not just tidiness.
- **eventbus PG-auth drift (P5) is a *separate* recurring failure** I'd not surfaced —
  the brain's own DB connection is intermittently broken, independent of the model.
</content>
