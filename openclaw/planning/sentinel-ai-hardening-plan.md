# Sentinel AI — Comprehensive Hardening & Reliability Plan

**Date:** 2026-06-15 · **Status:** plan (no code beyond what's noted as SHIPPED) ·
**Owner:** Sentinel AI pillar (OpenClaw + brain_store + the surfaces)

Consolidates every finding from: (1) the 3-reviewer hardening audit (reliability /
security / resilience), (2) the 2-agent deep tuning research (inference path + cross-surface
continuity), (3) the community/primary-source research (OpenClaw GitHub issues + docs +
HN), and (4) the **live production failure** in the roster thread on 2026-06-15.

---

## 0. Why now — the triggering reality

The owner's real assistant use (security-duty roster → reminders → calendar) is **actively
failing**. The live evidence:

- Thread `chat 0612-212917`: **8 of 42 messages are saved ERROR responses** (`[bridge_error]`,
  `status=None`, `Context overflow`, `request timed out`) that get **replayed as assistant
  context every turn** — a self-poisoning loop that compounds with each failure.
- A turn that ran **900 s** then died to the Python hard-kill fence — OpenClaw's own 600 s
  timeout never fired (the stall bug).
- A transient `ECONNREFUSED` (a brief Qwen eviction) became a **hard failure** instead of a
  retry/degrade.

These map 1:1 to community-reported issues (below). The platform is real and heavily used
(~720–780k weekly npm downloads) but widely criticized as bloated/buggy; we are not alone.

---

## 1. Architecture & the shared spine

```
 Surfaces:  TG DM ─┐   TG group forum-topics ─┐   Mini App / TWA / Tauri ─┐   Comet panel ─┐
                   │                          │                          │               │
                   ▼                          ▼                          ▼               ▼
            brain_store (Postgres :9433, schema brain)  ◄── ONE shared threaded conversation
                   │  (eventbus LISTEN/NOTIFY brain_events · /ws/brain push · mirrors)
                   ▼
            brain_wrapper.openclaw_one_shot  ──spawns──►  wsl node openclaw dist/index.js agent
                                                                 │   (embeds @agentclientprotocol/
                   ┌── persistent OpenClaw gateway :18789 ◄──────┘    claude-agent-acp runtime)
                   ▼
            MetaMCP :12008 ──► MCP servers      infer-bridge :8095 ──► llama-swap :1234 ──► Qwen 27B
                                                 (FIFO queue, GPU broker-gated)
```

**The point:** one weak link in the spine degrades **all** surfaces at once. Harden the spine,
not six skins. Two structural facts drive most findings: (a) a **per-turn spawned `agent`
process competes with the already-running persistent gateway** over session files; (b) the
embedded agent is a **Claude-designed ACP runtime** driving a local Qwen via a Node subprocess
— an impedance mismatch.

---

## 2. Findings by workstream (P0 = users stuck · P1 = degraded · P2 = hardening)

### 2A. Reliability — the request path (the "aborted" cluster)

| ID | Finding | Sev | Evidence | Fix |
|----|---------|-----|----------|-----|
| R1 | **Failed turns persisted as replayable assistant content** → self-poisoning loop | **P0** | thread `chat 0612-212917` 8/42 error-turns; embedded in the live `--message` history | Don't save error responses as replayable assistant rows (own surface/flag, exclude from `load_for_llm`); one-time cleanup of existing error-turns |
| R2 | **Orphaned in-flight turns — no reaper** | **P0** | 10 `streaming_done=false` rows, oldest 19 d; no startup/periodic sweep | Startup + periodic reaper finalizes `[interrupted]` + emits `message.complete`; per-turn Python timeout |
| R3 | **Stall doesn't self-abort** (OpenClaw 600 s ignored → 900 s Python fence) | **P0/P1** | live 900 s kill; GH **#71127** "stuck detected, never aborted"; `recovery=none` | Set native `diagnostics.stuckSessionAbortMs` (~600 s) **and** keep an external watchdog (community does both) |
| R4 | **`model.fallbacks` empty** — no failover | **P1** | `openclaw.json` `fallbacks:[]`; live `next=none` | Add a reachable fallback (OpenRouter or a small local model) **+ the #47705 guard (R5)** |
| R5 | **🚨 Sticky-fallback overwrite** — a successful fallback is persisted into `openclaw.json` and the primary is never retried | **P1** | GH **#47705** (hit 6× / 5 d) | Monitor `agents.list[].model` drift + force-reset to primary; know recovery (delete stale `~/.openclaw/agents/<agent>/sessions/`) |
| R6 | **Fallback does NOT fire on timeout / no cascade on 404** | **P1** | GH **#44936**, **#51209** | Retry-once on timeout/ECONNREFUSED/404 must be **our** code at the wrapper, not config |
| R7 | **Cold-load → silent failure / discovery-404** | **P1** | GH **#43946**; live `ECONNREFUSED`; 30 s model-discovery preflight hard-fails ≥400 | Keep Qwen warm (llama-swap `preload`+`ttl`, partly set) **+ a pre-turn warm-gate** that covers **first-token** latency (no native `connectTimeoutMs`, GH **#41371**) |
| R8 | **Timeout coupling fragile** | **P1** | idle-watchdog collapses to 120 s if either field unset; official guidance "raise provider `timeoutSeconds` first" | Set provider + `agents.defaults.timeoutSeconds` together at **900** (cover cold-load 210 s + gen); add per-model `requestTimeoutMs` |
| R9 | **infer-bridge `QUEUE_MAX_WAIT_S=170`** sized to the OLD 180 s ceiling | P1 | `infer_bridge.py:77` | Raise to ~870 to track the 900 s run timeout |
| R10 | **No turn-level Python watchdog** | P1 | TG `_run_turn` + bridge `_run_turn` trust OpenClaw to self-abort | `concurrent.futures` ~200 s+ fence that force-finalizes the reserved row (pairs with R2) |
| R11 | **Context-budget enforcement fuzzy** | P2 | live "Context overflow" turn; summariser `except`-skips when LLM down | Enforce a hard pre-send budget; verify the wrapper 32 KB preamble cap vs `reserveTokensFloor` don't double-trim |
| R12 | **No `statement_timeout`/pool on long agent DB conns** | P2 | per-call `psycopg.connect`, no pool | `connect_timeout` + `statement_timeout`; health-gate POST on a fast DB ping |

### 2B. Security — owner-only but partly public; address TODAY

| ID | Finding | Sev | Evidence | Fix |
|----|---------|-----|----------|-----|
| S1 | **Comet panel = account-takeover path** — unauth bridge → full-tool agent (Gmail/Finance/Shopping/files) + active-tab Playwright + `sandbox:off`, no approvals; "localhost-only" bypassable via DNS-rebinding | **Critical** | `comet-sidepanel/bridge.py` (no auth, no Host check), `mcp_server.py:202`, `exec-approvals.json {}` | Bridge token + `Host` validation + **tool-scope** the browser agent (strip Gmail/Finance/file-write) + re-enable approvals for state-changing tools |
| S2 | **World-readable secrets** in `~/.openclaw/openclaw.json` (cleartext tokens) | High | was mode 0644 | **chmod 600 — SHIPPED 2026-06-15** (interim); **ROTATE** the bot/MetaMCP/portfolio/Azure tokens (read during review) — PENDING |
| S3 | **`/api/brain/*` unthrottled** (only `/api/auth/*` rate-limited) | High | `bridge.py` `_rate_check` only in auth handlers | Global per-IP/session token-bucket in `before_request` + cap concurrent in-flight turns |
| S4 | **`MINI_APP_SECRET` served public** → pre-auth gate is theater | High | `bridge.py:3263-3298` injects it into the page | Stop treating it as secret; rely on initData-HMAC/TOTP/passkey only |
| S5 | **`/api/notify` XFF-spoofable** if `NOTIFY_TOKEN` unset | Med | `_client_ip` trusts `X-Forwarded-For` | Use `request.remote_addr` for the loopback check; require `NOTIFY_TOKEN` |
| S6 | **TG owner-ACL fail-open default** | Med | `owner_id=None` → "anyone can chat" | Fail closed — refuse to start without an explicit owner id |
| S7 | Comet bridge returns `stderr_tail` to client (token-leak amplifier) | Low | `comet-sidepanel/bridge.py:110` | Strip secrets from error responses |
| — | **Context:** OpenClaw was weaponized in a Feb-2026 supply-chain attack (compromised Cline CLI installed it to abuse broad perms) — runs with dangerous privileges → S1/S2 are MORE urgent | — | CSO Online | — |

### 2C. Resilience / Observability

| ID | Finding | Sev | Evidence | Fix |
|----|---------|-----|----------|-----|
| O1 | **The turn-executing TG bot isn't a monitored service** | **P0** | absent from `service_catalogue.yaml`; bot death = silent loss of TG turns + mirrors + cron | Add `tg-shared-brain-bot` catalogue entry + deep health probe |
| O2 | **"Responsive but useless" wedged OpenClaw undetected** — no end-to-end canary | **P0** | live 12-min wedged turn probed green; probes are TCP/HTTP-200/passive-journal | Synthetic canary: a tiny prompt through llama-server every ~5 min, assert non-empty reply |
| O3 | **Postgres NOTIFY not durable** — events lost on consumer disconnect | P1 | `eventbus.py` fire-and-forget, no replay | Persist `brain.events` + replay-on-reconnect; `pullTail` on WS open |
| O4 | **Brittle env-load on restart** → DB auth break | P1 | **caused today's 2-min incident**; `POSTGRES_PASSWORD` template-clobber **FIXED (commit 5407eac)**; the bot still relies on a `findstr` .bat | Add `_load_env_local()` to `brain_store.py` import (covers bot/mirror/scheduler) |
| O5 | **cp1252 mojibake in logs** | P1 | `getUpdates errored: �` in `sharedbrain_bot_task.log` | `PYTHONIOENCODING=utf-8` + `PYTHONUTF8=1` in the launcher |
| O6 | **Cron cold-wakes abort on 180 s** | P1 | `scheduler.py:47` `_JOB_TIMEOUT_S=180`; live `mrt-status-daily aborted` | Pre-warm before scheduled jobs + raise cron timeout to 600 + retry-once |
| O7 | **Unbounded brain_store growth** — no retention/VACUUM | P2 | only SQLite auth sessions purged | Retention job + prune `brain.events` + scheduled VACUUM |
| O8 | **Partial turn-id correlation** | P2 | no surface→bridge→openclaw→llm join | Thread one `turn_id` through every layer + log line |

### 2D. Continuity — the cross-surface conversation feature

| ID | Finding | Sev | Evidence | Fix |
|----|---------|-----|----------|-----|
| C0 | **DM→topic mirror — SHIPPED + verified** (commit 770ab0d) | done | owner test 2026-06-15 | — |
| C1 | **Symmetric topic→DM will LOOP** — echo-guard can't distinguish directions | **P1** | `_is_mirror_echo` = content+10 s+`surface!='telegram'` | Provenance dedup via **`surface_msg_id`** (already in schema, **unused** — single highest-leverage change) |
| C2 | **"Prefer-topic" routing makes the DM unreachable as a mirror target** | **P1** | `mirror.py:166`, `tg_user_mirror.py:68` `ORDER BY (tg_topic_id IS NOT NULL)` | Directional routing by trigger surface: `telegram-grp`→DM, `telegram-dm`→topic, `miniapp`→both |
| C3 | **DM single-window vs many threads** — auto-repoint corrupts continuity | **P1** | DM has its own active thread | DM mirrors = **read-only echoes** with `[via #topic]` prefix; only switch on explicit reply-to |
| C4 | Echo-guard false +/− (identical short msgs, late Telethon, edits) | P2 | `dispatcher.py:481` | Fold into C1 provenance dedup |
| C5 | Media (image/PDF/voice) doesn't cross surfaces (text-only) | P2 | both mirrors send `content` only | Placeholder lines now; real `sendPhoto`/`sendDocument` later |
| C6 | Silent message loss on mirror failure — no retry/DLQ | P2 | `mirror.py:208` drop-on-fail | Outbox/DLQ table + sweeper; clear stale topic bindings on 400 |
| C7 | Ordering / double-notifications / no cross-surface typing | P2 | separate processes; reply can outrun the user msg | Sequence user-msg→reply; `disable_notification` on mirrored-in copies |
| C8 | Archived/closed-topic + group "General" mirroring edge cases | P2 | `topic_sync`, `dispatcher.py:512` | Filter bindings by topic liveness; decide General's role |
| C9 | Additions: mirror on/off toggle, bindings indicator, loop circuit-breaker, chunk parity | P3 | — | Nice-to-have after C1–C3 |
| C10 | **IMPROVE:** native `session.identityLinks` for the DM↔Mini-App slice | opt | docs; bug GH **#31440** | Collapse DM+MiniApp to one canonical key → shrinks the custom bridge to just the group-topic mirror |

---

## 3. Strategic decision — OpenClaw: patch-tune-route vs. own the loop (Option B)

- The 180 s is **not** a hard ceiling — config already raised the run timeout to 600 s; the
  remaining friction is the embedded **Claude-agent-ACP runtime** + the **spawn-per-turn vs
  persistent-gateway** race (`EmbeddedAttemptSessionTakeover`, GH #84460/#83510).
- **Pivotal move (R-structural):** route turns **through the persistent gateway's RPC**
  instead of spawning a competing `agent` one-shot. Community-confirmed as the root-cause fix;
  it also unlocks real concurrency (`maxConcurrentSessions`) and removes the host-wide turnstile.
- **Option A (patch & tune & route):** do §2A + the gateway-routing. Keeps OpenClaw's
  accumulated Qwen know-how. Cost: maintain patches/config against a fast-moving, buggy upstream.
- **Option B (own a Python in-process loop):** drop the Claude-agent-ACP-via-Node layer for a
  direct OpenAI-tool-calling loop behind `brain_wrapper` (your stack is already Python; the
  loop runs in-process in the bridge → no WSL subprocess, native async, your own
  timeout/fallback/concurrency). Contained swap (one component), not a rewrite.
- **Recommendation:** do the **cheap reliability pack (§2A) now** — it's validated and fixes
  what's actively breaking. Treat **gateway-routing** as the bridge step. Decide **Option B**
  deliberately afterward, A/B'd against OpenClaw on a scratch thread. **Do NOT migrate to
  Nanobot reflexively** — its "fix" is amputation (does less), with no evidence it solves the
  stall/cold-start root causes, and our continuity + GPU-broker layer would need rebuilding.

---

## 4. Sequenced roadmap

**Phase 0 — Immediate relief (today, minutes)**
- Clean the 8 error-turns from `chat 0612-212917` (or `/new`). Owner can `/new` for instant use.

**Phase 1 — Reliability pack (kills the "aborted"; mostly our code)**
- R1 error-turn-poisoning fix + cleanup → R3 stall self-abort (`stuckSessionAbortMs` + watchdog)
  → R7+R8 warm-gate + timeout coupling → R6+R4+R5 retry-once + fallback **with the #47705 guard**
  → R2+R10 reaper + turn-level fence → R9 queue re-couple → R11 context budget.

**Phase 2 — Security (today)**
- S1 Comet bridge (token + Host check + tool-scope + approvals) → S2 rotate tokens → S3 rate-limit
  → S4 drop MINI_APP_SECRET-as-secret → S5/S6 notify XFF + owner fail-closed.

**Phase 3 — Resilience / observability**
- O1 monitor the bot → O2 end-to-end canary → O4 brain_store env self-load → O5 cp1252 → O3
  eventbus durability → O6 cron pre-warm → O7 retention.

**Phase 4 — Continuity (a)**
- C1 provenance foundation (`surface_msg_id`) → C2 directional routing → C3 read-only DM echoes
  → C6 DLQ + C9 loop breaker (safety net) → C4/C7/C8 → C5 media → C10 identityLinks.

**Phase 5 — Structural**
- Gateway-RPC routing (kills the turnstile + takeover race) → evaluate Option B.

---

## 5. Verification matrix

- **R1:** new failures do NOT appear as assistant turns in `load_for_llm`; poisoned thread answers fast after cleanup.
- **R3:** a deliberately-wedged turn aborts at ~600 s with a clean error, not 900 s.
- **R4/R6:** kill Qwen mid-turn → the turn degrades to a fallback/retry, not `next=none`; primary auto-recovers (no #47705 stick).
- **R7:** a cold-start turn warms-then-answers (bounded wait) instead of a discovery-404.
- **S1:** a crafted local page cannot drive the Comet agent without the bridge token; state-changing tools require approval.
- **O2:** stop llama-server → the canary flips the AI pillar red within one interval.
- **C1–C3:** typing in a topic appears in the DM (read-only, `[via #topic]`) with NO reply loop; a DM reply still routes to the DM's own thread.

## 6. Out of scope (now)
Full Option-B rewrite (decide after Phase 5); real media mirroring (C5 placeholders first);
ntfy push / SSE fallback / voice-in (the 6 feature enhancements — separate track, after hardening).
</content>
</invoke>
