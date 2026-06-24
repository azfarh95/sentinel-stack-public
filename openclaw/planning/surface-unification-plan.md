# Sentinel AI — Surface unification plan (one web bridge, one auth, one brain)

**Date:** 2026-06-15 · **Status:** PLAN (no code) · **Decision:** owner chose "full
bridge/auth unification first" before adding the browser-assistant mode.
**Why a plan, not a patch:** this is a **load-bearing auth refactor** — done wrong it
locks the owner out of every web surface or silently breaks the Mini App. Execute it
fresh + tested, surface-by-surface, with `:8101` kept alive as rollback until verified.

---

## 1. Today — 3 entry paths to OpenClaw, fragmented auth

| Path | Process | Auth | OpenClaw path | brain_store? | Streaming |
|---|---|---|---|---|---|
| **Telegram** (DM + topics) | `SentinelSharedBrainBot` | owner-ACL (`owner_id`) | `dispatcher → brain_wrapper.chat_turn` | ✅ shared | TG typing |
| **Web app** (Mini App / TWA / Tauri / WebUI) | `sentinel-miniapp-v2/bridge.py` **:8098** (Flask) | **mature**: initData-HMAC + TOTP + passkey + owner-token + session (`before_request`) | `chat_turn_begin/finish → brain_wrapper` | ✅ shared | `/ws/brain` push |
| **Comet panel** | `comet-sidepanel/bridge.py` **:8101** (raw http.server) | **NONE** (S1) + CORS-open | **own** `run_openclaw_turn` spawn (DUPLICATE) | ❌ **silo** | **SSE** `/events` token stream |

**The sprawl is really:** (a) the Mini-App family = ONE web UI in 4 wrappers (TWA wraps
the URL, Tauri embeds `dist/`, WebUI is the page); (b) the Comet panel = a separate
unauth bridge with its own OpenClaw spawn off the shared brain. TG stays native.

## 2. Target — 2 bridges, 1 web auth, 1 brain

```
TELEGRAM (DM+topics) ── owner-ACL ──┐
                                     ├──► brain_wrapper.chat_turn ──► brain_store ──► OpenClaw spine
WEB BRIDGE :8098 (one auth) ─────────┘
  ├─ Mini App / TWA / Tauri / WebUI  (thin wrappers of one UI)
  └─ Browser panel (was :8101)       ── "browser mode" (browser-use), tool-scoped + approval-gated
```

- **One web bridge (`:8098`)** with the existing mature auth gates EVERY non-TG surface.
- **Comet panel becomes a `:8098` client** — its chat routes through `brain_wrapper`
  (shared `brain_store`, a `comet`/`browser` surface tag), not its own spawn.
- **`:8101` retired** (process dropped; S1 hole closed by inheriting `:8098` auth).
- The **browser-use mode** is then added on `:8098` — NOT a 4th bridge.

## 3. Migration sequence (each step reversible; `:8101` stays until step 5)

1. **Streaming parity on :8098.** Decide: add an SSE `/api/agent/stream?session=`
   endpoint to the miniapp bridge (mirrors `:8101`'s `/events`), OR switch the Comet
   panel to the existing `/ws/brain` push. (SSE is the smaller change for the panel.)
   Behind `before_request` auth.
2. **Comet chat → brain_wrapper.** Replace `comet-sidepanel`'s `run_openclaw_turn`
   with a call to the bridge's `chat_turn_begin/finish` (so Comet turns land in
   `brain_store` under a `comet` surface + a dedicated thread). Removes the duplicate
   spawn + the host-wide turnstile contention.
3. **Repoint + auth the panel UI.** Point the Comet side-panel frontend at `:8098`
   (was `:8101`), carrying a session token (owner-token → `/api/auth/device` → session,
   the same flow the Mini App uses). Tool-scope the browser agent to browser tools only
   (strip Gmail/Finance/file-write) + approval gate on state-changing actions (S1 fix).
4. **Verify every surface** (test matrix §5) against `:8098` while `:8101` still runs.
5. **Retire `:8101`** (stop the `comet-sidepanel` bridge task; archive the file). Update
   `service_catalogue.yaml` (drop the `:8101` entry; the bot/`tg-shared-brain-bot` add
   from O1 pairs here).
6. **(Then, separate)** add the **browser-use mode** on `:8098` — Milestone 1 of the
   browser-assistant build lands as a mode of the now-unified web surface.

## 4. Risks + mitigations (auth refactor = handle with care)

- **Lockout** — a broken `before_request` change kills ALL web surfaces. Mitigation:
  change auth ADDITIVELY (the panel gets a session like the Mini App; don't touch the
  existing gates); keep an owner-token escape hatch; test on a scratch route first.
- **Break the Mini App** — it shares the bridge. Mitigation: the panel routes are NEW
  additions; don't refactor existing `/api/brain/*` or `/chat` in the same pass.
- **Streaming regression** — the panel relies on token SSE; the Mini App on WS push.
  Mitigation: add SSE alongside (don't replace WS); verify panel streaming before retire.
- **Brain fusion** — Comet turns now enter `brain_store`. Decide the surface tag +
  whether the Comet thread is its own or shares the DM thread (likely its OWN `comet`
  thread — do NOT fuse into the DM, per the C-continuity lessons).
- **Rollback** — `:8101` + the old panel config stay until §5; reverting = repoint the
  panel back + restart the old bridge.

## 5. Test matrix (before retiring :8101)

- Each surface authenticates + completes a chat turn via `:8098`: Mini App, TWA, Tauri,
  WebUI, **Comet panel**.
- Comet turn appears in `brain_store` (its own thread, `comet` surface) + streams tokens.
- An UN-authed request to the panel routes is **401** (S1 closed).
- Browser agent (when added) cannot reach Gmail/Finance/file-write; state-changing
  actions require approval.
- Mini App + WS push unchanged (no regression).

## 6. Out of scope (now)
The browser-use mode itself (separate Milestone 1, lands on the unified bridge); merging
the TG bot into the web bridge (it's native Telegram — stays its own process); media
streaming across surfaces.
