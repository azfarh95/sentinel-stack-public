# Browser Assistant (B) — comprehensive phased plan

**Target architecture:** Config C — **Qwen-27B brain + DOM-index primary grounding + UI-TARS-1.5-7B
grounder fallback (DOM-blind only)**, built **A/B-first**, **approval-gated**, CDP-attached to the real
Comet, landing as a **"browser mode" on the unified `:8098` bridge**. (ADR AI-011.)

**Stack-awareness (the load-bearing constraint — from the Dove reliability work):** a browser task makes
**many sequential LLM calls on a SINGLE `-np 1` inference slot**, and the grounder lane (Phase 5) triggers
**Qwen⇄UI-TARS model swaps** = cold-loads. So B directly stresses the three things we just hardened:
the single-slot wedge, llama-swap load/health, and turn-fencing. Every phase below carries that load in mind.
Relevant existing machinery to reuse, NOT reinvent: the **GPU broker** (`:8200`, arbitrates the 24 GB card +
the `:8095` FIFO queue), **infer-bridge `:8095`** (2.3 fast-503 + single-flight + the broker block/queue),
**keep-warm** (`ttl 86400`), the **model-server supervisor** (auto-recovery, dry-run→arming), and the
**turn-fence + reaper** pattern (3.3). The shared **`brain_store`** is the continuity layer.

---

## Guiding principles
- **Build incrementally; let reality pull the next piece.** A/B (Qwen + DOM) may be the whole product —
  don't pre-build the grounder/swap until a real DOM-blind wall is hit (the gateway-RPC/load-gate lesson).
- **B must never destabilise Dove.** Chat reliability is the senior tenant of the GPU; B yields to it.
- **Every state-changing action is approval-gated** (S1). The agent is tool-scoped to browser tools only.
- **Each phase has explicit CHECKS (gates).** Don't advance on a red gate.

---

## Phase 0 — Harness hardening + the step/loop fence
**Goal:** turn the nucleus spike into a safe, bounded harness that can't hang or flood the slot.
**Work:**
- Wrap the browser-use `Agent.run` with a **step-fence + wall-clock fence** (`max_steps`, `max_wall_s`,
  per-LLM-call timeout) — the browser analogue of the 3.3 turn-fence. A runaway loop must self-terminate
  and free the `:8095` slot, not wedge it.
- Route the agent's LLM calls through **`:8095`** (NOT raw `:1234`) so they inherit the broker block/queue +
  2.3 fast-503 (a wedged backend fails the browser step fast instead of hanging the whole task).
- Structured run log: per-step action, ground-method (DOM vs fallback), latency, token counts, final status.

**CHECKS (gates):**
- [ ] Tier-1 nucleus still passes through the fenced harness (no regression).
- [ ] A **deliberately-stuck task** (e.g. an impossible goal) is killed by the fence within `max_wall_s` and
      the `:8095` active-count returns to 0 (confirm via `:8095/health` `active`).
- [ ] With the backend wedged (simulate), a browser step gets a fast 503 and the task aborts cleanly (no 10-min hang).

---

## Phase 1 — Tier-2/3 capability validation (Config A/B, DOM-based)
**Goal:** find the **reliable ceiling** of local Qwen-27B + DOM on real tasks, before any infra investment.
**Work:**
- Build a **task suite** (~10–15 tasks/tier): T1 read/extract, T2 form-fill (with an approval *stub*), T3
  multi-tab / short workflows. Use stable test sites + a couple of your real targets.
- **Vision A/B:** run each tier with `use_vision=False` vs `True`; measure success-rate delta **and** the
  latency cost (a screenshot is up to ~8 k image tokens of prefill **per step** on `-np 1` — quantify it).
- Record where it breaks (the failure taxonomy → tells us if/when a grounder is needed).

**CHECKS (gates):**
- [ ] T1 ≥ 90%, T2 ≥ 70% success on the suite (numbers to calibrate, not dogma).
- [ ] Documented **vision cost**: median extra seconds/step with `use_vision`, and the tiers where it *changes
      the outcome* (so we only turn it on where it pays).
- [ ] A written **failure taxonomy**: % of failures that are *reasoning* (model), *DOM-blind* (needs grounder),
      or *harness* (fixable) — this **gates whether Phase 5 is even needed**.

---

## Phase 2 — Inference coexistence with Dove (the reliability-critical phase)
**Goal:** prove B can run on the shared `-np 1` GPU **without starving or destabilising chat (Dove)**.
**Work:**
- Characterise the load: a browser task = N sequential Qwen calls. With `-np 1` they **serialise** with chat
  turns. Decide + implement the coexistence policy:
  - Browser LLM calls go through `:8095` → the **broker FIFO queue** orders them with chat (chat is not starved;
    it queues, with the 2.3 fast-503 as the backstop).
  - Treat a browser *session* as a deliberate **foreground** activity (owner-initiated); chat turns interleave
    via the queue rather than being blocked for the whole session.
- Confirm the **model-server supervisor + keep-warm** are not destabilised by sustained browser load (no new
  wedges, no false auto-recovery trips).

**CHECKS (gates):**
- [ ] **Concurrency test:** a long browser task running, fire a chat turn → chat completes within an acceptable
      queue wait (measure), does NOT hang or error.
- [ ] Over a 30-min browser soak: **zero** new `model_server` wedge detections / false auto-recovery trips
      (check the supervisor telemetry / audit), and Qwen stays resident (keep-warm holds).
- [ ] `:8095` never deadlocks under interleaved browser+chat load (active-count returns to baseline).

---

## Phase 3 — C: surface-unification (the integration prerequisite)
**Goal:** the load-bearing auth refactor so B can land on `:8098` (per `surface-unification-plan.md`).
**Work (each step reversible; `:8101` stays until step 5):** streaming parity on `:8098` → route Comet chat
through `brain_wrapper` (shared `brain_store`, `comet` surface) → repoint+auth the panel UI on `:8098` →
verify every surface → retire `:8101`. **Additive auth changes only** (the panel gets a session like the Mini
App; don't touch existing gates).
**CHECKS (gates):** the surface-unification test matrix —
- [ ] Each surface (Mini App, TWA, Tauri, WebUI, Comet panel) authenticates + completes a turn via `:8098`.
- [ ] An **un-authed** request to the panel routes → **401** (S1 closed).
- [ ] Mini App + `/ws/brain` push **unchanged** (no regression).
- [ ] `:8101` retired only after all green; rollback (repoint + restart old bridge) verified to work.

---

## Phase 4 — Integrate B as the "browser mode" on `:8098`
**Goal:** the real, gated, brain-integrated browser assistant.
**Work:**
- The fenced browser-use agent (P0) behind a `:8098` route, **CDP-attached to the real Comet** (launch via
  `Launch-Comet-CDP.ps1`, attach `cdp_url=:9222`); throwaway-headless remains an option for unattended runs.
- **Tool-scope:** browser tools ONLY — strip Gmail / Finance / file-write from this agent's toolset.
- **Approval gate:** every state-changing action (click/type/submit/navigate-away) → owner approval through the
  bridge (reuse the Mini App's owner-token/approval channel); read/extract auto-proceed.
- Browser turns land in **`brain_store`** under their **own thread + `browser` surface** (do NOT fuse into the
  DM thread — the C-continuity lesson).

**CHECKS (gates):**
- [ ] **Tool-scope enforced:** the browser agent demonstrably cannot reach Gmail/Finance/file-write (attempt → denied).
- [ ] **Approval gate fires** on a state-changing action and **blocks** until owner approval; a read does NOT prompt.
- [ ] An **end-to-end real task** (e.g. extract + fill-with-approval) completes via `:8098`, appears in
      `brain_store` (own thread, `browser` surface).
- [ ] **No regression to Dove**: chat reliability metrics unchanged during/after a browser session.

---

## Phase 5 — DOM-blind fallback (ONLY IF Phase-1 taxonomy proves it's needed)
**Goal:** cover the DOM-blind tail (canvas / image-of-text / obfuscated DOM) the DOM index can't click.
**Field consensus (web review 2026-06-16):** the production pattern is **DOM-primary + vision/parser fallback**
— a real **12–17 pp reliability gap favors DOM**, and vision costs **~10–20×** more — i.e. **Config C is the
consensus**, not an off-piste choice.

**Fallback options, RANKED by fit-to-THIS-stack (reliability first, accuracy second):**
- **5a (PREFERRED) — a non-LLM PERCEPTION stack + the existing Qwen (set-of-marks).** Use **OmniParser**, OR a
  DIY **PaddleOCR/Tesseract (OCR) + a light detector (Grounding DINO / DETR)** → numbered, structured elements;
  **Qwen picks an element by ID** (not pixel coordinates). This is the **"mmproj-style" bolt-on perception
  layer**: it **reuses Qwen → NO model swap**, and runs on **CPU / a separate process → minimal `-np 1`
  contention** with chat. Best fit for the Dove single-GPU reliability constraint. Ceiling ~40% on hard
  screens; adds OCR/detection latency per step.
- **5b (ESCALATION) — a dedicated grounding/agent model as a llama-swap lane.** `UI-TARS-1.5-7B` (GGUF exists)
  or the newer `Fara-7B` (screenshot-only 7B computer-use). Higher grounding (~50–61%) BUT triggers a
  Qwen⇄grounder **cold-load swap** = a model-server lifecycle event (supervisor-watched, broker-arbitrated) +
  needs **VL-on-llama.cpp/ROCm validation**. Use ONLY if 5a's accuracy is insufficient on real tasks.

**Work:** implement **5a first**; wire browser-use grounding to fall back to it for DOM-blind targets; measure;
escalate to **5b** only on a proven accuracy gap.

**CHECKS (gates):**
- [ ] **5a:** click accuracy on a DOM-blind test set meets a bar (e.g. ≥ 50% single-shot; retries close the
      gap); perception latency acceptable; **no measurable `-np 1` contention with chat** (it's CPU/separate).
- [ ] **5b (if escalated):** grounder click accuracy on ROCm/llama.cpp validated (*coordinates*, not just that
      it loads); the **Qwen⇄grounder swap completes cleanly** (supervisor sees a normal load, no wedge/false
      auto-recovery); swap-back to Qwen for chat is reliable.

---

## Phase 6 — Hardening, observability, rollout
**Goal:** make B safe to leave on.
**Work:** task-level telemetry (success rate, steps, ground-method mix, swap events, approval rate, vision
usage); the step-fence + a stuck-session reaper (mirror 3.3); rate/scope guards so a browser session can't
monopolise the GPU indefinitely; docs (ADR AI-011 update + runbook) + a rollback path.
**CHECKS (gates):**
- [ ] A multi-hour soak: no GPU monopolisation, no Dove degradation, no leaked browser sessions.
- [ ] Observability shows task success-rate + the DOM-vs-grounder mix; alerts on a stuck browser session.
- [ ] Rollback verified (disable browser mode → chat + surfaces unaffected).

---

## Dependencies & sequencing notes
- **P0–P2 are standalone** (the harness) — they de-risk the *capability* + *reliability-coexistence* questions
  BEFORE the load-bearing auth refactor. Do them first.
- **P3 (C) is the gate to P4.** It can run in parallel with P0–P2 if desired, but B can't *land* without it.
- **P5 is conditional** on the P1 failure taxonomy showing real DOM-blind need — otherwise skip it (A/B is the product).
- **The senior constraint throughout:** the single `-np 1` slot + Dove's reliability. If any phase's checks show
  B destabilising chat, stop and fix the coexistence before proceeding.

---

## Appendix — Landscape & candidate tools (web review 2026-06-16)
**Harness (we use `browser-use`) — alternatives considered:** **Stagehand** (TS, AI primitives on Playwright —
the architecture template others copy), **Skyvern** (vision-based, best open agent at *form-filling*),
**LaVague** (research), **Steel** (cloud-browser infra). browser-use stays our pick (Python, CDP-native,
leading WebVoyager ~89%) — but its hybrid pattern mirrors Stagehand's, which is the consensus shape.

**Perception / grounding (the "mmproj-analog" modular layer):**
- *Parser (preferred fallback, 5a):* **OmniParser** (detection + OCR → set-of-marks); DIY **PaddleOCR/Tesseract +
  Grounding DINO/DETR**. Non-LLM, local, CPU-friendly, reuses Qwen.
- *Grounding/agent models (escalation, 5b):* **UI-TARS-1.5-7B** (GGUF), **Fara-7B** (screenshot-only),
  **UI-Venus-Ground-7B**, **GTA1-7B**, **MolmoPoint-GUI-8B**.

**Consensus that validates Config C:** hybrid **DOM-primary + vision/parser fallback**; vision-only is ~12–17 pp
less reliable and ~10–20× costlier where the DOM exists. So: DOM-index first, perception-stack fallback,
grounding-model only if needed.

**Sources:** model research → `sentinel-watchdog/logs/browser-grounding-model-research-2026-06-16.md`; web review
→ Stagehand/Skyvern framework comparisons, microsoft/OmniParser, OCR comparisons (PaddleOCR/Tesseract/docTR),
WebSight (vision-first arch), Fara-7B (arXiv 2511.19663).
