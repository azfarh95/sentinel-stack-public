## Sentinel Generation Roadmap

**Narrative**: works → controllable → powerful → reliable → shareable-as-alpha → portable → multi-host.

| V | Theme | What it unlocks | Status |
|---|---|---|--------|
| **V1** | Prototype | Agent works at all | ✅ done |
| **V2** | Operability | Manageable from a phone | ✅ shipped (v2.0.0–v2.13.0, 14 releases) |
| **V3** | Capability | Useful as a daily personal AI assistant | 🟡 in progress |
| **V4** | Reliability | Stable enough to share — load testing, session hardening, docs | ⚪ next |
| **V5** | Guest alpha | Invited outsiders try MY stack in isolated, capped, revokable sessions | ⚪ Phase-A guest caps already shipped in v2.12.0 — rest pending |
| **V6** | Portability | Outsiders run their OWN install on their machine | 🟡 prep started 2026-05-09 (Phases A/B/C complete; D/E pending) |
| **V7** | Multi-tenant | An install hosts multiple equal users, persistent isolated state, BYO model | ⚪ designed, parked |

---

## V2 — Operability *(complete)*
- [x] OpenClaw Config panel (reasoning effort, max tokens, timeout, web toggles)
- [x] OpenClaw Doctor panel (systemd, port checks, logs)
- [x] OpenClaw Skills — enable/disable + inline credential manager (Windows Credential Manager)
- [x] Version system: `VERSION` file + `bump_version.ps1` + `/api/version` + Settings footer
- [x] GitHub Actions auto-version workflow
- [x] docs/ folder: miniapp, watchdog, config, llm-prompt, CONTRIBUTING, LICENSE
- [x] #24 — Fix DNS apex `your-domain.example.com` (CNAME-flattened to subdomain via Cloudflare DNS, 2026-05-09)
- [ ] #25 — Watchdog: escalate persistent DNS failures (apex removed from `dns_watch`, escalation logic still TODO)
- [ ] #27 — Icon library (Lucide) + preset themes (Midnight/Ocean/Slate/Warm)

## V3 — Capability *(in progress)*

**Shipped:**
- [x] V3 Browser panel: CDP screencast in mini app (v3.0.0)
- [x] Drive Mode: tap-to-click + keyboard input via CDP (v3.1–3.3)
- [x] Chat composer: send-as-owner via Telethon (v3.1.5)
- [x] CDP screencast over polling (v3.4.0) — 8x mobile bandwidth reduction at v3.4.1
- [x] Stealth flags + cookie import via Netscape `cookies.txt` (v3.5.x)
- [x] LM Studio model autosync (`scripts/sync_lm_models.py` + bridge background thread)
- [x] V3 Maps panel + maps-mcp container
- [x] Reminders system (Telegram triggers + reminders-mcp)

**Queued for V3 (from 2026-05-09 research benchmark + Claude expansion):**
- [ ] **Real cross-session memory** — five distinct guarantees (verified-write, manual search, importance pinning, quota-aware, hybrid retrieval). The #1 user-cited gap. ~2-3 weeks.
- [ ] **Voice round-trip** — phone-records → Whisper → Qwen3.6 → Piper TTS → audio reply. ~2-3s end-to-end achievable. ~3-5 days.
- [ ] **Calendar optimization** — auto-block focus, conflict-detect, travel-aware. Beyond CRUD. ~1 week.
- [ ] **Proactive nudges** — opt-in per category, mute-able from any notification, traceable ("triggered by event X"). ~1-2 weeks.
- [ ] **Unified context engine** — aggregate calendar + location + GPU load + recent activity into one snapshot per turn. Foundational for proactive features. ~1 week.
- [ ] **Email triage + voice-mimicked drafts** — daily digests, replies in user's authentic tone (50-shot from past emails in memory store). ~1 week.

Reference: `workspace/proposals/2026-05-09-V3-Wishlist-Findings.md` for synthesis from the dual-agent research mission. `workspace/research/V3-handheld-AI-wishlist-{sentinel,expanded}.md` for source material.

## V4 — Reliability *(planned next after V3)*
- [ ] Load testing — sustained agent workload over 24-72 hours
- [ ] Session hardening — graceful recovery from LM Studio crashes, OpenClaw restarts, Cloudflare Tunnel disconnects
- [ ] Memory-leak watch — automated detection across the 12-MCP-container stack
- [ ] Long-run reliability instrumentation — dashboard for uptime / error rates / per-tool failure modes
- [ ] #28 — MkDocs Material GitHub Pages docs site
- [ ] OpenClaw context compaction at 80% of loaded window (queued from 2026-05-09 benchmark; fixes silent truncation)

## V5 — Guest alpha access *(next after V4)*

Goal: invited outsiders can try MY running stack via temporary, isolated, capped sessions. Like an alpha-tester onboarding flow.

- [x] Phase-A guest caps shipped early in v2.12.0 (token quotas, time limits)
- [ ] Invite/revoke flow — generate shareable invite tokens with TTL + scope
- [ ] Per-guest session storage — isolated from owner's data; auto-expires
- [ ] Guest visibility policy — what tools / skills / files are visible vs hidden
- [ ] Audit log — who did what during their session, retained on owner's side
- [ ] Per-guest rate limits — protect GPU + API quotas from abuse
- [ ] Mini-app guest UI — separate entry point at `/guest/<invite-token>`

This is *narrower* than full multi-tenant. Guests don't get equal-citizen status, can't BYO their own model, can't persist long-term state. They taste the agent, then leave.

## V6 — Portability *(prep started 2026-05-09)*

Goal: by V10, single-command install on a fresh Windows machine. V6 is the prep + first installer.

**Hard blockers** (will never be inside the installer; will chain via `winget`):
- Docker Desktop, WSL2, LM Studio (license + size), the model weights (size), per-machine secrets (Telegram tokens, OAuth refresh tokens, etc.)

**V6 prep — hardcoded paths + identifiers audit (73 violations across 22 code files):**
- [x] Phase A — `_paths.py` central paths module (13 violations fixed, 4 files migrated)
- [x] Phase B — script-relative paths in `.bat`/`.ps1`/`.py` (35 violations fixed)
- [x] Phase C — drop personal-ID fallback defaults (10 violations fixed)
- [ ] Phase D — drive-letter parameterization (G:\ → `$env:USERPROFILE`-relative or env-overridable, 10 violations)
- [ ] Phase E — fresh-user-account smoke test (gates V6 release candidate)

Aggregate: **58/73 violations fixed (79%)** as of 2026-05-09.

Reference: `workspace/proposals/2026-05-09-V6-MSI-Feasibility.md`, `workspace/proposals/2026-05-09-V6-prep-Hardcoded-Paths.md`.

**V6 build phases (after V6 prep complete):**
- [ ] Step 1: Bootstrap PowerShell installer (`install.ps1`) — chains `winget` for prereqs, clones repo, sets up WSL OpenClaw, registers scheduled tasks, drops user into mini-app first-run wizard. ~1 weekend.
- [ ] Step 2: Extend bridge.py first-run wizard — token prompts, Cloudflare Tunnel auto-creation via API, `lms get` model download triggers, container health verification. ~2-3 days.
- [ ] Step 3: MSI proper (Inno Setup) — wraps bootstrap script, bundles source, handles uninstall. After bootstrap is rock-solid on fresh VMs. ~1-2 weeks.

## V7 — Multi-tenant *(designed, parked)*

Goal: a Sentinel install hosts multiple **equal-citizen** users (not just guest alpha). Each tenant has persistent isolated state, can BYO model, has their own secrets vault.

- [ ] Per-tenant secrets in cloud (Drive / OneDrive appdata vs Vaultwarden — open decision)
- [ ] BYO model per tenant — tenant A on Qwen3.6, tenant B on Llama 3, etc.
- [ ] Agent isolation — tenant A's memory invisible to tenant B
- [ ] Tenant lifecycle — onboarding, suspension, data export
- [ ] Shared-vs-private resource allocation (GPU time, MCP-server access)

Builds on V5 (isolation model proven) and V6 (clean install story). Reference: `workspace/proposals/sentinel-v5-multitenant/` (folder name historical — was tagged V5 in earlier drafts; reframed as V7 in 2026-05-09 reorder).

---

## Mini-app feature backlog (not a Sentinel generation)

These are mini-app-specific items, scoped smaller than a generation step. Track here, ship as v3.x.x point releases or independent tickets:

- **Android Capacitor wrapper** — wrap existing HTML/CSS/JS Mini App in a native Android shell. Replace Telegram initData auth with PIN / biometric. Backend unchanged. *(De-listed from V-numbered roadmap 2026-05-09 — was V4 in earlier draft; reframed as mini-app deployment target, not a Sentinel generation.)*
- iOS Capacitor build (downstream of Android — same wrapper, different target)
- PWA installability polish (manifest tuning, offline shell)

---

## Sibling projects (NOT a Sentinel generation)

These live in `workspace/proposals/Projects-Proposal-WIP/V4/` (the "V4" folder name is historical and predates the V4 = Reliability decision — it's just a directory). They orbit Sentinel but ship as independent products:

- **ClaudeAssistant** — Two-container QA env (testbot + testlogger). Built; needs BotFather token + test group ID to launch. Used today as @SentinelClaudeAssistantBot for owner reminders.
- **ClaudeTG2fa** — Two-bot Telegram bridge for Claude Code with 2FA-gated tool execution. Proposal only.
- **VideoDownloader** — yt-dlp + gallery-dl Docker MCP server. Built and running in MetaMCP Media namespace. Today's Instagram-stories bug fix landed here.
- **smdl (Standalone)** — yt-dlp + gallery-dl with SQLite cache + JSON config. VPS-portable. Completed 2026-05-04.

---

## Persistent infrastructure added since this roadmap was first written

- **`workspace/inventory/`** — single source of truth for what's running. INDEX.md + architecture.yaml (static) + violations.yaml (V6 prep tracker) + running.yaml (auto-refreshed via `scripts/refresh_inventory.py`).
- **`workspace/benchmarks/`** — agent-run benchmark database with `scripts/extract_benchmark.py` to generate entries from any OpenClaw trajectory. First baseline: 2026-05-09 V3 wishlist research.
- **`workspace/reminders/`** — verbose backups behind compact Telegram reminders. Per the two-tier rule: reminders/notifications/summaries are compact in-message; reports are .md documents.

---

## Nice to have (no committed generation)
- Remote theme pack loading / community theme manifests
- Print service ("can you print this for me?") — proposal at `workspace/proposals/2026-05-09-HA-Print-Service.md`. Not in any committed V slot.

---

*Last reorganized 2026-05-09 — established the V1-V7 sequence: prototype → operability → capability → reliability → guest-alpha → portability → multi-tenant. Earlier drafts had Android in V4 and conflicting V5/V6 definitions; cleaned up to make each generation a meaningful capability shift built on the prior.*
