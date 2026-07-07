# Sentinel Stack — V1 & V2 History (Verbose)

**Purpose:** complete, no-summary record of what existed in V1 and what was built in V2. Written for future context (next session, future contributor, future you). Not pushed to GitHub — local workspace reference.

**Date written:** 2026-05-09
**V1 status:** archived
**V2 status:** closed at v2.14.0 on 2026-05-09 (14 minor releases shipped over ~2 days of intense work)

---

## V1 — Pre-versioning era (before 2026-05-07)

V1 wasn't called "V1" while it was happening — it was just "the Sentinel stack." The label is retroactive. The defining marker is `commit 46948d8 feat: add version system` which became `v2.0.0` and started everything that gets called V2.

### What existed in V1

**Telegram entry points (two-bot system):**

- `@YourSentinelBot` — main AI assistant bot, powered by OpenClaw (Claude-based agent)
- `@YourWatchdogBot` — separate admin/watchdog bot, runs the management plane

The two-bot split was deliberate: the AI bot is for users, the watchdog bot is for ops (restart commands, alerts, system status). They use different bot tokens and live in different processes.

**Core stack components (all running from V1):**

| Component | Role | Port |
|---|---|---|
| MetaMCP | Central MCP gateway, aggregates all tool servers | 12008 |
| OpenClaw | Claude-based agent in WSL2 Ubuntu-24.04, the actual brain | 18789 |
| LM Studio | Local LLM hosting (Qwen 2.5 9B and Qwen 3.6 27B for some period) | 1234 |
| Inference Bridge | Transparent HTTP proxy in front of LM Studio | 8095 |
| Sentinel Bridge (mini app v1) | Dashboard backend for the v1 single-page mini app | 8097 |
| Watchdog Status Server | Read-only `/status` JSON endpoint for the mini app | 8099 |
| Reminders MCP | APScheduler-based scheduled Telegram reminders | 8087 |
| Memory MCP | Long-term memory storage for the agent | 8092 |
| yt-dlp MCP | Video download tool | 8088 |
| Google Workspace MCP | Gmail, Drive, Calendar | 8089 |
| Maps MCP | Google Maps geocoding/directions | 8090 |
| GitHub MCP | GitHub repo + issue tools | 8091 |
| Translate MCP | LibreTranslate wrapper | 8094 |
| OneDrive MCP | OneDrive + Document Intelligence | (varies) |
| smdl (Nanobot) | Standalone yt-dlp + gallery-dl downloader | (varies) |
| Crib Watchdog Power Monitor | HA-WebSocket-based power spike tracker, separate from the main Sentinel watchdog | (varies) |

**Supporting infrastructure:**

- Cloudflare Tunnel exposing the mini app at `https://your-domain.example.com`
- Postgres for MetaMCP backend
- Docker Compose orchestrating the MCP container fleet
- WSL2 systemd-managed `openclaw-gateway.service`
- Windows Task Scheduler running `infer_bridge.py`, `bridge.py` (mini app), `watchdog.py`

**Mini App v1:**

Single-page dashboard at port 8097. Used a simple sentinel-token auth model (token embedded in the page via Cloudflare worker). No two-factor, no session management.

What it could show:
- Service health (port checks)
- Disk usage
- LM Studio current model
- Inference active/idle indicator

That was about it. No watchdog interactions, no model selection, no reminders/memory views.

**Watchdog v1:**

Worked, but was strictly an alert system. It monitored containers, ports, OpenClaw, LM Studio, and sent Telegram alerts when things went down. Auto-restart was implemented but limited. No CRITICAL escalation, no log-tail-on-failure, no inline restart buttons in the dashboard.

**Reminders v1:**

`reminders-mcp` existed, exposed `add_reminder`, `list_reminders`, `cancel_reminder`. APScheduler-backed with SQLite persistence. Multi-recipient was NOT supported — every reminder went to one chat_id.

**Memory v1:**

`memory-mcp` existed and was working. The owner could store memories that OpenClaw could later retrieve. SQLite + tags.

**Inference Bridge v1:**

Just a transparent HTTP proxy at `localhost:8095/v1` forwarding to LM Studio at `localhost:1234/v1`. Three reasons it existed:
1. **Detection point** for the crib watchdog power monitor — counts active inferences so spikes can be classified `ai_inference` vs gaming/abnormal
2. **Block point** for power-conflict protection (later added in V2)
3. **Routing layer** for multi-model classification (also later)

In V1 it had only role #1.

**Tags from the V1 era:**

Both pre-versioning tags:
- `miniapp-v1` — initial single-page Sentinel dashboard with simple auth
- `miniapp-v2` — `Fix MemoryMCPClient._call() to handle multiple content items` (this was named `miniapp-v2` but predates the formal V2 series — it's a tag from the V1 era, confusing)

The line between V1 and V2 = the introduction of the `VERSION` file + bump script + `/api/version` endpoint at `46948d8`, which became `v2.0.0`.

### V1 known issues that motivated V2

- No formal versioning — couldn't say "what version are you running"
- Mini app v1 had only a sentinel-token auth (single-factor, weak)
- No way to restart things from the mini app, only from Telegram commands
- No critical-failure escalation — if a service went down and auto-restart failed, you only knew via the periodic check
- No skill credential management — API keys were in random `.env` files
- No GitHub Actions auto-version workflow
- Documentation was sparse (just the top-level README)
- Reminders couldn't notify multiple people from a single rule
- LM Studio + gaming on the same 7900 XTX could brown out the 650W PSU; nothing was preventing concurrent load
- OpenClaw had occasional duplicate-systemd-unit kill-loops (user-level + system-level)
- Telegram bot got 409 conflicts when multiple OpenClaw instances polled
- No way to add new model providers without editing OpenClaw config files
- Approving guests required SSH-ing into WSL to run `openclaw pairing approve`
- No per-user usage tracking; one shared model serves all approved users

V2 set out to fix all of the above, and did.

---

## V2 — The 14-release sprint (2026-05-07 → 2026-05-09)

V2 = "Telegram bot infrastructure → production-ready Sentinel."

### V2 themes (what we were optimising for)

1. **Polish + UX** — make the mini app a real dashboard people use, not a status display
2. **Operability** — restart things, see logs, edit config without SSH
3. **Security** — TOTP, WCM-backed secrets, scanned commits
4. **Power safety** — interlock against gaming + AI inference simultaneously (real risk on 650W PSU + 7900 XTX)
5. **Multi-user readiness** — even though V5 multi-tenant is parked, V2 lays the foundation: contact registry, per-tester caps, OpenRouter integration
6. **Documentation** — every major component has a `docs/<x>.md`

### Per-release breakdown

#### v2.0.0 — Sentinel Mini App v2 (the foundation)

- **Mini App v2** rewrite: Telegram identity (HMAC-SHA256 on `initData`) + TOTP (Google Authenticator, 30s window, rate-limited 5 attempts / 15 min → 429)
- **8-hour session tokens** stored as SHA-256 hashes with per-row HMAC integrity in SQLite (direct DB edits detected via MAC mismatch)
- **TOTP setup flow** via local-only `totp_setup.html`
- **OpenClaw Config panel** — read/write OpenClaw model, provider, agent settings without SSH
- **OpenClaw Doctor panel** — live diagnostic: systemd service state, last 10 log lines, MetaMCP connectivity, tool count, memory count
- **Watchdog restart buttons** on Docker containers + AI stack
- **CRITICAL escalation** — repeated failures get louder Telegram alerts with last 20 log lines
- **Version system** — `VERSION` file + `bump_version.ps1` + `/api/version` endpoint + Settings footer
- **Auto-version GitHub Actions** — feat→minor, fix→patch, BREAKING→major

#### v2.1.0 — Skill Credential Manager

- Tap any skill in Settings → expand → enter API token → stored in **Windows Credential Manager**
- `setup_secrets.ps1` for first-time secret configuration

#### v2.2.0 — Updates screen + cookie refresh

- **Updates panel** — grouped version checker (AI Core, MCP Gateway, Media, Language, Platform)
- **`scripts/refresh_cookies.ps1`** — auto-detects default browser (Chrome/Edge/Brave/Arc/Comet) and dumps cookies for yt-dlp
- **LLM install & restore guide** (`docs/llm-prompt.md`)
- **Mini App screenshots gallery**

#### v2.3.0 — Reminders multi-recipient

- `add_reminder` MCP tool gets `recipients: [chat_id, …]` parameter
- Single reminder fans out to one primary + N extras
- Failures on individual recipients logged, don't abort siblings

#### v2.4.0 / 2.4.1 / 2.4.2 — Reminders multi-recipient (cont.) + patch fixes

- Watchdog bot now responds to `/start` from non-owners (preparation for contact registry)
- Patch script fixes: real user home under sudo, OpenClaw pairing message wording
- LF normalisation (`.gitattributes` enforces LF for `.sh`/`.py`)

#### v2.5.0 — Gaming / inference power conflict protection

The largest single architectural addition of V2. Three components moving together:

- **Inference bridge gains `POST /infer_block` and `POST /infer_unblock` endpoints**
- **`_active_connections: list`** — tracked HTTPConnections on every inference. When `/infer_block` fires, ALL in-flight connections are closed → LM Studio streaming aborts mid-tokens
- **Power monitor** detects Steam gaming via Home Assistant entity (`sensor.steam_steam_…`); on game start it POSTs `/infer_block` + sends Telegram "Inference: blocked ⛔". On game end: unblock + "Inference: unblocked ✅"
- **OpenClaw duplicate-units detection** — watchdog detects user-level + system-level `openclaw-gateway` both active, alerts via Telegram (this was a real recurring problem)
- **Mini App `oc_dupe_conflict`** — Processes panel shows "OpenClaw: duplicate units" when conflict is live
- `/infer_status` response now includes `blocked` field

#### v2.6.0 — Contact registry + reminder contact dropdown

- Watchdog saves non-owner `/start` users to `watchdog/contacts.json` (chat_id, first_name, username, registered_at)
- `_merged_contacts()` unions local registry with OpenClaw's pairing data
- Watchdog `/status` endpoint exposes `contacts: [...]`
- New `/api/contacts` endpoint in mini app bridge
- Reminders form gains "Send to" row: Me / Group / Contacts (multi-select dropdown)
- `target=contacts` + `contact_ids[]` in `/api/reminders` POST

#### v2.6.1 — OpenClaw pairing patch (deprecated by v2.7.0)

- Patched `~/.openclaw/.../pairing-messages-os97WTVG.js` to fire-and-forget POST contact info to watchdog on every `/start`
- This approach was fragile — global `fetch` in bundled ESM, runtime issues
- Replaced by reading OpenClaw's pairing JSON directly in v2.7.0

#### v2.7.0 — Contacts via OpenClaw pairing store

The cleaner replacement. No patch needed.

- Watchdog reads `~/.openclaw/credentials/telegram-pairing.json` directly via WSL UNC path (`\\wsl.localhost\Ubuntu-24.04\…`)
- Anyone who `/start`s the AI bot appears automatically in the Mini App contact picker
- Removed the `pairing-messages.js` patch

#### v2.7.1 — Reminders architecture documentation

- New `docs/reminders.md` — APScheduler architecture, decoupled firing, schedule formats, multi-recipient, restart resilience, cost/power table
- Key insight documented: reminders fire at **0 tokens, 0 GPU, ~50ms** regardless of how they were created. Reminders work even when LM Studio is down or gaming is active.

#### v2.8.0 — Pending Pairings card + reminders cleanup

- **Pending Pairings card** in mini app Settings — lists everyone in `telegram-pairing.json` not yet in `allowFrom`, with one-tap **Approve** button (runs `openclaw pairing approve telegram <code>` via WSL exec)
- **No more SSHing into WSL** to approve guests
- **Reminders cleanup** — daily 04:00 cron job in APScheduler purges `completed`/`cancelled` reminders older than 30 days; runs once on startup so containers off for a while catch up

#### v2.8.1 — Pairing approve path fix

- `openclaw` binary wasn't on the WSL non-interactive PATH for `bash -lc`
- Hardcoded absolute path `/home/azfar/.npm-global/bin/openclaw` in the bridge subprocess call

#### v2.9.0 — OpenRouter Auto preset

- Added `openrouter/free` (auto-router across free models) as the first preset in the Add OpenRouter Model dropdown

#### v2.10.0 — Auto-discover existing OpenRouter key

- Mini app prompted "API key required" even when user already had a key configured in OpenClaw at `~/.openclaw/agents/main/agent/auth-profiles.json`
- Bridge now checks discovery order: WCM first (canonical) → OpenClaw `auth-profiles.json` (legacy)
- When found in auth-profiles only, key is mirrored to WCM on first model add

#### v2.10.1 — Gitignore contacts.json

- `watchdog/contacts.json` contains real Telegram chat IDs and names — must never commit

#### v2.11.0 — 3-way model routing + availability check

- New `CODING_MODEL = "qwen/qwen2.5-coder-32b-instruct"` route alongside SIMPLE_MODEL (qwen3.5-9b) and COMPLEX_MODEL (qwen3.6-27b)
- Triggered by code blocks (` ``` `) or `_CODING_KEYWORDS` (function, refactor, debug, regex, typescript, rust, etc.) — coding > complex > simple precedence
- Bridge queries LM Studio `/v1/models` (with WCM-stored Bearer token) before rewriting requests
- Falls back gracefully when intended model isn't loaded:
  - CODING → COMPLEX → SIMPLE
  - COMPLEX → CODING → SIMPLE
  - SIMPLE → COMPLEX → CODING
- `/infer_status` now reports `loaded: [...]` and corrects displayed `_current_model` if cached value isn't loaded
- Designed for 24 GB VRAM cards (RTX 3090/4090, 7900 XTX) — only one of {27B chat, 32B coder} loaded at a time, LM Studio swaps on demand

#### v2.11.1 — Refresh button + loaded models list

- Inference card gains `↻` refresh button + "Loaded: <models>" line under active-model badge
- `/infer_status?force=1` query param invalidates the 15s loaded-models cache for user-initiated refresh
- Empty state: "No models loaded — open LM Studio to load one"

#### v2.12.0 — Per-tester daily message caps (V5 Phase A scaffolding)

Shipped early because shared-OpenRouter-key beta needs it.

- New `watchdog/guest_caps.py` module — SQLite-backed daily counter per `chat_id`
- Default cap **50 messages/day** per guest, configurable per tenant
- Source of truth: OpenClaw's `sessions.json` `lastInteractionAt` field — polled every 60s, increments usage when it advances
- When a guest hits cap → bot removes them from `allowFrom` (OpenClaw stops responding) → owner gets Telegram alert
- At midnight local: counters reset, throttled guests restored automatically
- **Guest Usage card** in mini app Settings — live progress bar per guest (green / amber / red)
- Tap any row → Telegram-native popup with 20 / 50 / 100 cap presets + custom number
- THROTTLED badge for currently-capped guests; ↻ refresh button

#### v2.13.0 — Guest Usage UX fixes

- Inline cap editor (replaces `tg.showPopup` / `window.prompt` which are unreliable in Telegram WebView)
- Watchdog reads contact names from OpenClaw `sessions.json` `origin.label` field; parser handles 3 label formats: `"Name (@user) id:X"`, `"Name (@user)"`, bare `"Name"`
- Owner filtered out of guest views (`/api/contacts`, `/api/guests/usage`)
- Each row shows display name + `@username · chat_id` subtitle

#### v2.13.1 — chore release for tag bumps (no functional change)

#### v2.14.0 — V2 closing release: secret hygiene + docs

- 6 plaintext secrets in `.env.local` migrated to Windows Credential Manager under `sentinel-miniapp` service: `better_auth_secret`, `telegram_bot_token`, `smdl_bot_token`, `github_pat`, `onedrive_client_secret`, `docintel_key`
- `.env.local.template` (committed) holds structure with `__WCM_<key>__` placeholders
- `scripts/sync_env_from_wcm.ps1` regenerates `.env.local` from WCM at boot — atomic write, auto-backup
- `START_AI_STACK.bat` runs sync as step `[0/7]` before any docker compose command
- Sanitized previously-committed plaintext: `Maintenance/CONNECTIVITY_SCAN.md` and `Maintenance/TROUBLESHOOTING.md` had literal `sk-lm-…` tokens → replaced with `<LMSTUDIO_APIKEY>` placeholders
- `send_ig.py` was tracked from before its `.gitignore` rule → `git rm --cached` untracks it (file remains on disk)
- `scripts/check_secrets.ps1` pre-commit scanner — greps staged files for known patterns (sk-or-, sk-lm-, ghp_, AIza, tvly-, hf_, telegram tokens, AWS, Slack); installed at `.git/hooks/pre-commit`
- `docs/inference-bridge.md` — 3-way routing, gaming/inference power lock, availability resolver, HTTP/1.0 streaming
- Dependabot triage: all 58 open alerts are MetaMCP upstream (apps/backend, apps/frontend, pnpm-lock.yaml) — nothing actionable on our side

### V2 metrics

- **14 minor releases** over ~2 days of intense work
- **All 14 backfilled with proper GitHub release notes**
- **6 secrets migrated to WCM**
- **5 docs added/updated**: README, miniapp.md, watchdog.md, reminders.md (new), inference-bridge.md (new), config.md
- **3 bots** in production: AI bot, Watchdog bot, smdl bot
- **~15 services** orchestrated (Docker + WSL systemd + Windows Task Scheduler)
- **10+ MCP servers** active

### V2 architectural inventory (final state)

```
Windows side:
  - infer_bridge.py             :8095   (Python, runs as pythonw.exe via Task Scheduler)
  - sentinel-miniapp-v2/bridge.py :8098   (Flask)
  - watchdog/watchdog.py         :8099   (HTTP /status server)
  - LM Studio                    :1234   (Qwen 27B / Qwen Coder 32B swap pair)
  - Cloudflare Tunnel            (your-domain.example.com)

WSL2 (Ubuntu-24.04):
  - openclaw-gateway.service     :18789  (systemd, OpenClaw agent)
  - LibreTranslate               :5050

Docker:
  - metamcp                      :12008
  - reminders-mcp                :8087
  - ytdlp-mcp                    :8088
  - google-workspace-mcp         :8089
  - maps-mcp                     :8090
  - github-mcp                   :8091
  - memory-mcp                   :8092
  - translate-mcp                :8094
  - onedrive-mcp                 (varies)
  - smdl                         (varies)
  - firefly III                  :8180
  - postgres                     :5432
```

### V2 storage map (final state)

| Where | What |
|---|---|
| Windows Credential Manager | Owner secrets — bot tokens, API keys, TOTP, OpenRouter, LM Studio, GitHub PAT, OneDrive client secret |
| `~/.openclaw/credentials/` (WSL) | OpenClaw's own pairing/auth files — telegram-pairing.json, telegram-default-allowFrom.json, auth-profiles.json |
| `metamcp-local/.env.local` | Generated by sync script from WCM — gitignored |
| `metamcp-local/.env.local.template` | Committed structure with `__WCM_<key>__` placeholders |
| `watchdog/contacts.json` | Local contact registry override (gitignored) |
| `watchdog/guest_usage.db` | Per-tester daily message counters (SQLite, gitignored) |
| `reminders-mcp /data/scheduler.db` | APScheduler job table (in container volume) |
| `reminders-mcp /data/reminders.db` | Reminder metadata (in container volume) |
| `~/.openclaw/agents/main/sessions/sessions.json` | OpenClaw's session-to-chat-id binding (used by guest_caps.py) |

### V2 carried forward into V3+

These are deliberate scaffolding choices made in V2 that V3/V5 will build on:

- **WCM as canonical for owner secrets** — V3+ should treat it as immutable, not introduce a parallel store
- **`auth-profiles.json` discovery** — bridge can find existing keys here; V5's per-tenant secrets will not touch this file (which is owner-only)
- **Three-way inference router** — extensible if V3 adds e.g. a "creative writing" route
- **Guest cap counter (Phase A)** — when V5 ships, the cap counter switches from `lastInteractionAt` polling to actual MCP-gateway-side counting
- **`allowFrom` toggle as enforcement primitive** — V5's tenant deactivation reuses this
- **Mini app session-token model** — V5's tenant self-service `/me` endpoints will reuse the same session validation

---

## Open V2 follow-up items (not blocking; useful to remember)

These didn't block V2 closing but were noted during the work:

1. **`~/.openclaw/openclaw.json` plaintext keys** — the LM Studio key and Tavily key still live there in plaintext. They're in `auth-profiles.json` too (canonical OpenClaw location) but the redundant copy in `openclaw.json` should be removed.
2. **History rewrite for `send_ig.py` token** — we untracked the file but the original Telegram bot token is still in old commits. Either rotate the bot token (easier) or use `git filter-repo` to strip it from history (more invasive).
3. **`models.json` had stale `lmstudio.apiKey`** — different value than the canonical one in `auth-profiles.json`. Should be removed since OpenClaw uses auth-profiles as source of truth.

---

## What V3 starts with

(For reference — full V3 proposal at `workspace/proposals/2026-05-08-Sentinel-V3.md`)

V3 = mini app feature panels:

1. **Embedded Google Maps panel** — Maps JS API directly in mini app; directions input, place search, restricted referrer-locked API key
2. **In-app Playwright browser** — screenshots streaming via WebSocket, click/keypress replay; one Playwright context per session
3. **Crypto wallet panel** — Tier 1 read-only dashboard (ETH/BNB/SOL balances, CoinGecko price feed); Tier 2 signing wallet (TPM/encrypted keystore, TOTP-gated)

V3 is "user-facing features" while V2 was "operational foundation."

V5 multi-tenant is parked behind V3 — full design at `~/.claude/projects/Projects-Proposal-WIP/V5/MultiTenant/`.
