# Phase 5 — Structural fix for the agent loop (scope)

**Date:** 2026-06-15 · **Status:** SCOPE (spike-gated; no migration yet)
**Why:** The turn-wedge/abort class is structural, not a timeout-tuning problem.
Raising timeouts (R8) is a band-aid — slow turns become slow *completions*, not fast
turns. This scopes the real fix.

---

## 1. The structural cost (what we're removing)

`brain_wrapper.openclaw_one_shot` **spawns a fresh `wsl node openclaw … agent` per
turn**, with a fresh session UUID and the whole thread re-inlined as `--message`. So
every turn pays:
- **cold process spawn** (Node + WSL hop) + the **host-wide turnstile** (a named mutex
  serialises spawns to avoid `EmbeddedAttemptSessionTakeover` against the persistent
  gateway) — i.e. we run a *competing* one-shot beside an already-running gateway;
- **full-preamble prefill** every turn (stateless: brain_store history re-inlined) +
  **per-step re-prefill** (tool-search mutates the catalog → KV-cache miss each step);
- no control over **streaming / timeouts / retries / concurrency** (we inherit the
  CLI's behaviour, incl. the 600s-not-firing stall).

Layer on TTL cold-loads + `-np 1` and "multi-step turn after idle" is *structurally*
slow. None of that is fixable by config.

## 2. The two options

### Option A — Gateway-RPC routing (reuse OpenClaw, stop spawning)
Route turns to the **persistent gateway** (`:18789`, already warm) instead of spawning a
one-shot. Removes: cold-spawn, the turnstile, the session-takeover race; unlocks the
gateway's `maxConcurrentSessions`.
- **Keeps** OpenClaw's hard-won agent loop, tool-search/progressive-disclosure (AI-003),
  skills, memory, the ACP runtime's robustness, the persona handling.
- **Cost / risk:** keeps the fast-moving **buggy upstream** + the Node/WSL layer; does
  **not** converge with the browser-use / Python direction.
- **VIABILITY = SPIKE-GATED** [Likely-but-unconfirmed]: the gateway serves a Control-UI
  SPA, so the GET-200s on `/rpc` `/v1/agent` are probably SPA fallbacks, not the agent
  API. The dist's `AgentRun` + `GatewayCommandRegistry` + plugin-HTTP-route-registry
  suggest a real submit path exists. **Spike 1 must find + prove it.**

### Option B — In-process Python tool-calling loop (own the loop)
Replace the `node agent` spawn with a Python loop inside the bridge:
`Qwen (:8095) ↔ tool-calling ↔ MetaMCP tools (:12008/metamcp/default/mcp)`, driven by
the SOUL persona, behind the existing `brain_wrapper`.
- **Wins:** in-process, native async, **full control** of timeout/retry/stream/concurrency;
  no WSL subprocess, no turnstile, no upstream churn; **converges** with browser-use
  (itself a Python in-process loop) and the surface-unification (one web bridge) — *one
  agent-loop architecture for chat + browser*.
- **Cost / risk** [the honest part]: you **re-implement what OpenClaw gives** —
  progressive tool-disclosure (AI-003's ~56k→~13k prompt win is non-trivial to rebuild),
  the agentic loop + recovery, skills, persona/workspace handling, multimodal (vision),
  and the robustness OpenClaw has iterated on. Underestimating this is the classic
  "rewrite the framework" trap.
- **Tool access = clean** [Certain]: MetaMCP's `:12008/metamcp/default/mcp` is the same
  aggregated toolset; a Python MCP client lists/describes/calls it (incl. the per-thread
  namespace overrides already in brain_store).

## 3. The convergence (why this isn't an isolated decision)

Three threads point the same way: **browser-use** (chosen) is a Python in-process loop;
**surface unification** (chosen) collapses to one web bridge; **Option B** is a Python
in-process loop for chat. Option B = one coherent in-process-agent architecture across
chat + browser, on the unified bridge. **Gateway-RPC keeps the Node/ACP layer** —
faster to ship, but a different architecture than where the other two are going.

## 4. Decision framework + recommendation

Sequence risk; don't bet big blind:
1. **Spike both** (below) — they're cheap and they *are* the decision.
2. **Recommended path:** **Gateway-RPC first** as the low-risk structural win (kills
   spawn/turnstile/race, reuses the loop) **IF Spike 1 proves the contract**. It stops
   the structural bleeding without a rewrite, and buys time to let **browser-use prove
   the in-process-loop pattern** on a contained surface.
3. **Then decide Option B deliberately** — after browser-use is real and the gateway-RPC
   A/B has data. If browser-use + unification land well, Option B becomes the natural
   convergence for chat too; if gateway-RPC is solid and Option B's re-implementation
   cost is confirmed high, stay on gateway-RPC. **Do NOT** pre-commit to Option B before
   the spikes + the browser-use experience.
4. If **Spike 1 fails** (no usable gateway submit API) → Option B becomes the default
   structural path (the spawn model has no cheaper fix).

## 5. Spikes (gate the decision — do these first, on a scratch thread)

- **Spike 1 — Gateway-RPC contract.** Find the gateway's real agent-submit interface
  (dist `AgentRun`/`GatewayCommandRegistry` + the channel mechanism the built-in channels
  use; or OpenClaw docs). Prove: POST a turn to the warm gateway → get the JSON reply,
  **reusing the process** (no spawn), with our brain_store preamble + per-turn session
  model + ideally token streaming. Measure turn latency vs the spawn path.
- **Spike 2 — Option B nucleus.** A ~150-line Python tool-calling loop: Qwen `:8095` +
  an MCP client to `:12008/metamcp/default/mcp`, with **progressive disclosure**
  (search→describe→call, mirroring AI-003) on a handful of tools (calendar/reminders).
  Run the roster task. Measure: does Qwen drive it reliably *without* OpenClaw's harness?
  This sizes the re-implementation honestly.

## 6. Migration (for the recommended Gateway-RPC first; reversible)
1. Behind `brain_wrapper.openclaw_one_shot`, add a `_via_gateway_rpc()` path (feature-
   flagged); keep the spawn path as fallback.
2. A/B on a scratch thread: same prompts, compare latency + correctness + no turnstile
   contention + streaming.
3. Flip the flag for one surface (Mini App), watch, then all surfaces.
4. Remove the turnstile + the spawn path once stable.

## 7. Risks / rollback
- Gateway-RPC: the gateway becomes a hard dependency (no per-turn isolation) → its crash
  takes all turns; mitigate with the existing watchdog + a spawn-path fallback flag.
- Option B: scope blow-up (re-implementing the framework) → contain to the nucleus spike
  before committing; keep OpenClaw as the fallback engine during migration.
- Both: **does NOT fix** model quality (Qwen 27B ceiling) or the `-np 1` slot — those are
  separate (AI-005). This fixes the *spawn/preamble/turnstile* structural tax only.

## 8. Out of scope (now)
The migration itself (spike-gated); the model decision; `-np 2`; the browser-use build
(separate Milestone 1, but its in-process pattern informs the Option-B decision here).
