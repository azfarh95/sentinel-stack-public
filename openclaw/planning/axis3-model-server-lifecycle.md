# Axis-3 — Model-server lifecycle: the durable fix for the daily Dove outage

**Date:** 2026-06-15 (late) · **Status:** DESIGN (grounded by a live incident + forensic this session)
**Owner goal:** stop the recurring all-day Dove failures durably, not with another patch.
**Scope:** the Qwen model-server (llama-swap + llama-server) load/health/recovery lifecycle.
NOT the OpenClaw transport (axis-2, gateway-RPC — separate, proven viable) or the `-np 1`
slot (axis-1, AI-005).

---

## 0. Why this doc exists (the reframing)

A 5-stream forensic earlier today (ADR AI-008) pinned "Dove broken all day" on **GPU
contention**. Tonight, mid-spike, Dove was **fully down** with the **GPU FREE (23.3/24 GB)**
— so contention was NOT the live cause. A direct completion to `:1234` (bypassing ALL of
OpenClaw) 500'd after 160 s. The llama-swap log shows the real signature:

- `running qwen exited: health check timed out after 6m0s` — **×12 today**.
- A **clean, uninterrupted manual load = 42 s healthy** (llama-swap stopped, no other callers).
- `Unloading model, TTL of 1800s reached` **repeatedly today** → keep-warm (`ttl 86400`) was
  NOT effective for much of the day; the model idle-unloaded every 30 min.

This is a **third reliability axis the docs underweight: model-server lifecycle.** It is the
actual daily Dove-killer, and it is independent of axis-1 (slot) and axis-2 (spawn/transport).

## 1. Root cause (corrected — the OOM-spiral hypothesis is WRONG)

Initial hypothesis (concurrent `llama-server` spawns → 2×~16 GB → OOM spiral) is **refuted**:
with `-np 1` and a single llama-swap, concurrent requests **queue**, they do not spawn extra
servers (confirmed: no concurrent-spawn/OOM in the log; `infer_bridge.py` + `-np 1`). The real
compound:

| # | Factor | Evidence |
|---|---|---|
| C1 | **Cold-loads are FREQUENT** — model idle-unloads, so most turns/crons pay a cold-load | `Unloading model, TTL of 1800s reached` ×N today; keep-warm `ttl:86400` only became live after tonight's restart (`/running` now shows 86400) |
| C2 | **Cold-loads are FRAGILE — interrupted or contended** → exceed the 360 s health check → `llama-server` exits | `health check timed out after 6m0s` ×12; but a clean manual load = 42 s, so the load itself is fine when left alone |
| C3 | **Uncoordinated load/evict triggers** race each other and the broker | `scheduler._prewarm` → `127.0.0.1:1234` DIRECT (bypasses bridge + OpenClaw turnstile), `scheduler.py:152`; media-ai → `192.168.65.254:1234` DIRECT, `summarize.py`; broker issues `/api/models/unload` mid-flight, `gpu_broker.py:267-284`; `[ERROR] failed reading from gpuCh` near loads |
| C4 | **RAM headroom < model** during busy windows → a contended load can legitimately blow 360 s | free RAM 11.4 GB < weights 15.4 GB at probe time |
| C5 | **NO auto-recovery** — once wedged it stays down until a human intervenes | `llama-server` ∈ `NEVER_AUTO` (alert-only), `autorestart.py:47-53`; bridge `/health` is **decoupled from backend residency** (`infer_bridge.py:222-238`) so a wedged-but-alive backend reads "healthy" and nothing escalates |

**The failure loop:** model idle-unloads (C1) → a turn triggers a cold-load that gets
interrupted/evicted/contended (C2/C3/C4) → load exceeds 360 s → `llama-server` exits →
the next caller retriggers → spiral → **nothing auto-recovers it (C5)** → Dove is down for
HOURS until a human runs the clean restart.

> **Open question to confirm with instrumentation (cheap):** is C2 dominated by *interruption*
> (broker/TTL/competing-caller killing a load in flight) or *genuine slow load* under
> contention? Log every `llama-server` spawn/kill with timestamp + the trigger to disambiguate.
> The design below addresses BOTH so we don't have to guess.

## 2. The fix — three coordinated parts (priority order)

### 2.1 AUTO-RECOVERY (the centerpiece — would have prevented tonight's hours-long outage)
A model-server supervisor (in `sentinel-watchdog`, alongside the broker + auto-restart rails)
that probes the **REAL backend**, not the llama-swap process liveness, and on a confirmed
wedge runs the **clean recovery proven tonight (42 s)**:

- **Probe:** a cheap backend canary every ~30 s — `GET 127.0.0.1:5800/health` (the llama-server
  itself) OR a `max_tokens:1` completion to `:1234`. Healthy = 200 within a few seconds.
- **Wedge signature (debounced, N consecutive):** `:5800` connection-refused while llama-swap
  is up, OR canary failing/timeout, OR the log emits `health check timed out after 6m0s`.
- **Recovery action (the exact sequence I verified):**
  1. `taskkill /F` ALL `llama-server.exe` (clear any half-loaded/zombie).
  2. restart the `SentinelLlamaServer` task (fresh llama-swap → single `on_startup` preload).
  3. verify ONE clean load reaches healthy (`/running` shows the model + a canary passes).
- **Rails:** reuse the existing watchdog cooldown + crash-loop cap + flap cap (`autorestart.py`)
  so recovery itself can't storm. This is a **smart** recovery (not the blind 16 GB restart that
  put `llama-server` in `NEVER_AUTO`), so it's safe to automate — remove `llama-server` from
  `NEVER_AUTO` ONLY for this dedicated recovery path.
- **Escalate:** if 2 recovery cycles fail, alert (the genuine "needs a human" case — e.g. driver/
  disk fault), don't keep thrashing.

### 2.2 COORDINATE all load/evict through one authority (kill C3 — the interruption vector)
Today three callers drive `:1234` with no mutual awareness, and the broker can unload mid-load.
Make **every** load/evict pass one gate so a load can't be interrupted by an evict or a
competing caller:

- **Single load-gate:** a host-wide "model-load lease" (mirror the existing OpenClaw turnstile
  pattern — a named mutex / a broker-owned lock) that `scheduler._prewarm`, media-ai, and the
  broker's evict ALL must hold. While a load is in progress, evicts wait; while an evict is in
  progress, loads wait. No interleave.
- **Route the bypass callers through it:** `scheduler._prewarm` (`scheduler.py:152`) and media-ai
  (`summarize.py`, `MEDIA_AI_QWEN_URL`) must acquire the gate (or go through the bridge) instead
  of hitting `:1234` raw. Removes the TOCTOU races the agent found (`broker.py` gate-read vs call).
- **Broker:** on unblock, finish `_comfy_free()` BEFORE releasing the load-gate so Qwen always
  loads onto a clean card (`gpu_broker.py:319-326`) — closes the load-onto-contended-card window.

### 2.3 BRIDGE backend-awareness (kill C5's blind spot + give callers fast failure)
- **Probe + surface:** `infer_bridge.py` should probe `:5800`/`:1234` and expose backend state in
  `/health` (a NEW field, separate from process-liveness so it doesn't re-trigger the false-DOWN
  restart storm the decoupling was built to avoid). This is what lets the §2.1 supervisor SEE a
  wedged-but-alive backend.
- **Fast 503 + single-flight:** when the backend is down/loading, return `503 Retry-After`
  immediately instead of blocking callers for up to `PROXY_TIMEOUT=600 s` (`infer_bridge.py:357`);
  and coalesce concurrent cold requests behind one load (single-flight) so a herd can't pile.
- **Keep-warm verification:** assert the running llama-swap actually has `ttl:86400` (it idle-
  unloaded at 1800 s for much of today). Fewer cold-loads = fewer chances to hit C2.

## 3. Why this is the durable fix (not another patch)
- It removes the **interruption** vector (2.2), reduces **cold-load frequency** (2.3 keep-warm
  check), and — critically — makes the system **self-heal** in ~42 s instead of staying down for
  hours (2.1). The three patches already parked (3.2/3.3/3.4) handle the *brain-store/transport*
  failure modes; this handles the *model-server* one — the missing axis.
- It's all **our code** (watchdog supervisor + broker gate + infer-bridge), no upstream dependency,
  reversible, and gated by the existing watchdog rails.

## 4. Verification matrix
- **C5/recovery:** kill `llama-server` deliberately → the supervisor detects within ~1 min and
  restores a healthy backend automatically (canary passes) WITHOUT a human, within the rails.
- **C3/coordination:** trigger a broker evict during a `_prewarm` → they serialize (no mid-load
  kill); the log shows no `health check timed out` from interruption.
- **C2 latency:** a cold-load under normal daytime load completes < 360 s (target < 120 s) — and
  if it doesn't, the supervisor recovers it instead of spiraling.
- **C1:** confirm zero `Unloading model, TTL of 1800s` events over a day (keep-warm effective).
- **End-to-end:** 24 h with zero manual model-server interventions and zero >6-min Dove stalls.

## 5. Risks / scope
- The auto-recovery must be **idempotent + rail-guarded** (it force-kills + restarts a 16 GB
  service) — reuse the watchdog's crash-loop/flap caps; never recover more than N×/window.
- The load-gate must not deadlock (timeouts + a single owner); model the broker as the owner.
- Out of scope: axis-1 (`-np 1`/`-np 2`), axis-2 (gateway-RPC), model quality. This fixes only
  the model-server load/health/recovery lifecycle.
- **Confirm C2's interrupt-vs-slow split with the §1 instrumentation before tuning timeouts** —
  don't just raise `healthCheckTimeout` (that's the band-aid; a stuck load just stays stuck longer).

## 6. Suggested build order (each independently shippable + verifiable)
1. **2.3 instrumentation + keep-warm assert** (cheap, read-mostly) — confirm C2 mechanism + stop C1.
2. **2.1 auto-recovery supervisor** (highest leverage — ends the hours-long outages).
3. **2.2 load-gate coordination** (removes the interruption root cause so recovery rarely fires).
4. Bridge fast-503 + single-flight (caller UX; lower priority once 2.1/2.2 land).
