# Browser Assistant (track B) — foundation + nucleus spike

**Status:** NUCLEUS VALIDATED 2026-06-16 — local Qwen (27B) drives `browser-use` for a tier-1 read/extract task.

## What this is
Track B (the browser assistant, Comet-like): a **`browser-use`** Python in-process loop driving a
**CDP-attached Chromium/Comet**, LLM = **local Qwen** (`:8095`, OpenAI-compatible via infer-bridge),
**tool-scoped + approval-gated**. Per the owner's decision it lands as a *"browser mode" on the unified
`:8098` web bridge* — so the surface-unification (C) is its integration prerequisite
(`openclaw/planning/surface-unification-plan.md`).

## Foundation (contained — NOT in the live stack's system python)
- `.venv/` — `browser-use 0.13.1` (CDP-native via `cdp-use`, no playwright). Isolated so it can't perturb
  the bot/bridge/inference deps. `requirements.txt` is a freeze of it.
- `_spike_browser.py` — the nucleus spike (throwaway headless Chrome + Qwen).

## Nucleus spike result (2026-06-16) — **PASS**
Task: *"Go to example.com, report the `<h1>`."* → **"Example Domain"**, 2 steps, 58 s. Local Qwen produced
the structured `navigate`+`done` actions (with `add_schema_to_system_prompt=True`, the local-model
fallback for models without reliable native function-calling). **=> The make-or-break hypothesis (local
27B drives browser-use tier-1) is VALIDATED.** No flailing; clean.

## browser-use 0.13.1 API notes (changes a lot between versions)
- `ChatOpenAI(model="qwen/qwen3.6-27b", base_url="http://127.0.0.1:8095/v1", api_key=..., temperature=0,
  add_schema_to_system_prompt=True)` — point at local Qwen; the schema-in-prompt flag is essential for a
  local model.
- `Browser(cdp_url="http://127.0.0.1:9222" | executable_path=..., user_data_dir=..., headless=...)` —
  **attach to Comet-CDP (:9222)** for the real integration (launcher: `comet-sidepanel/Launch-Comet-CDP.ps1`),
  or launch a throwaway headless Chrome for spikes.
- `Agent(task, llm, browser)`; `await agent.run(max_steps=N)`; `history.final_result()`.

## Operations (P4 gated assistant + P6 hardening) — runbook

The assistant is usable three ways, all sharing the same fenced+gated core (`agent_runner.run_task`):

| How | Command | Notes |
|---|---|---|
| **Side panel** | load `extension/` in Comet/Chrome | the easy way — type a task, watch steps, approve **inline**; see below |
| CLI (ad-hoc) | `.venv\Scripts\python run.py "..."` | `--comet` attach real Comet, `--telegram` phone approvals, `--vision`, `--steps/--wall` |
| HTTP surface | `POST 127.0.0.1:8108/run` (X-Comet-Token) | always-on (TS task `SentinelBrowserSurface`); see below |
| Kill-switch | `.venv\Scripts\python mode.py off` / `on` / `status` | file-marker; disables CLI **and** surface live |

### The unified panel (one source, any surface) — convergence P3
`extension/` is **Sentinel Assistant**: a portable panel with two modes —
**🌐 Browse** (delegate a goal to the gated agent; live steps + inline approve/deny) and
**🛒 Shop** (fast price-sorted product search across Shopee/Lazada/Amazon/Challenger+Shopify via the
anti-bot scrapers → product cards). **One source, deployed to any surface:**

| Surface | How it loads | API base | Token |
|---|---|---|---|
| Chrome/Comet side panel | Load unpacked `extension/` | `http://127.0.0.1:8108` | `config.local.js` (gitignored) |
| Browser tab | `http://127.0.0.1:8108/app/?token=<tok>` | same origin (relative) | `?token=` → saved to localStorage |
| WebView (e.g. **Volery**) | load `…:8108/app/` over the tailnet | same origin | `?token=` once |

- The surface **serves the same panel** at `GET /app/` (allowlisted files only — **never** `config.local.js`,
  so the token can't leak). MV3 forbids remote scripts, so the extension keeps a *local copy of the same
  source*; the served `/app/` is for tabs/WebViews.
- The JS is surface-agnostic: it derives the API base from where it's served, feature-detects `chrome.*`
  (falls back to `sessionStorage`), and resolves the token from `window.COMET_BRIDGE_TOKEN` → `?token=` →
  `localStorage` (→ prompts if none).
- **Shop mode** hits `POST /shop` (a thin proxy to the shopping MCP `:8100/api/search`) — fast, no LLM.
- **Volery / any off-box surface needs the surface reachable over the tailnet.** `:8108` is loopback by
  default (`tailnet_expose:false`); exposing it (token-gated `tailscale serve --tcp=8108 → tcp://127.0.0.1:8108`)
  is a deliberate security step — the panel + approval gate + kill-switch are designed for it, but it widens
  the attack surface to the mesh. Flagged, not auto-enabled.

### The `:8108` surface (always-on, monitored)
- **Launched by** TS task **`SentinelBrowserSurface`** at logon (hidden via
  `sentinel-watchdog/scripts/launchers/hidden/SentinelBrowserSurface.bat`, **venv** python; restart-on-fail 3×/1min).
  Log: `sentinel-watchdog/logs/browser_surface_task.log`.
- **Monitored by** the watchdog — catalogued as `browser-assistant-surface` (port 8108, `ai` pillar,
  `tailnet_expose: false`). Restart: `Restart-ScheduledTask SentinelBrowserSurface` (or via the admin UI).
- **Routes:**
  - `GET /health` (open) → `{enabled, busy, stuck, current{task,elapsed_s,wall_s}, uptime_s}`
  - `GET /metrics` (token) → success rate, status mix, fence trips, approval rate, vision/ground mix, dur p50/p95
  - `POST /run` (token) `{task, mode:headless|comet, steps, wall, vision, channel:telegram|console|none}`
- **Auth:** `X-Comet-Token` (shared `COMET_BRIDGE_TOKEN` from `.env.local`). Loopback-only — never tailnet-serve.

### P6 guards (so it's safe to leave on)
- **Rate guard:** ≤ 30 accepted `/run` per rolling hour → `429`.
- **Wall clamp:** a requested `wall` is clamped to `[30, 600]s` — no unbounded GPU-hogging session.
- **One-at-a-time:** a 2nd concurrent `/run` → `409` (browser + the single `-np 1` slot serialize anyway).
- **Stuck reaper** (background thread): if the in-agent wall fence somehow fails, it **alerts** (testbot) at
  `wall+90s` (and flips `/health.stuck`), then **force-kills the process tree** at `wall+300s` (chrome children
  included → no leaked sessions); the TS task restarts the surface.
- **Telemetry:** every run is logged to `runs.jsonl`; `metrics.py` (and `/metrics`) roll it up. Brain mirror:
  `browser_turns.jsonl` + `brain_store` (own `browser-assistant` thread, `surface=browser`).

### Rollback
`python mode.py off` → CLI + surface refuse new runs (`503`); chat + every other surface untouched. Re-enable
with `mode.py on`. To fully stop the surface: `Stop-ScheduledTask SentinelBrowserSurface`.

## Next (conditional)
- **P5 — DOM-blind fallback** is *conditional* on a real DOM-blind wall (canvas / image-of-text). The P1
  taxonomy shows DOM-index handles the workload → **skip until needed**; then 5a (OCR/parser → set-of-marks,
  Qwen picks by ID, no model swap) first, 5b (UI-TARS swap lane) only on a proven accuracy gap.
