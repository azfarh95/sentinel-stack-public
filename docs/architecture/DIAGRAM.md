# Project Sentinel — Architecture (current state, 2026-05-11)

Three views: **tiers**, **deployment topology**, **network boundaries**.

---

## 1. Tier diagram (capability stack)

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  PROJECT SENTINEL                                                             │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                               │
│  ╔══════════════════════════════════════════════════════════════════════╗     │
│  ║  TIER 1 — AI  (the "brain")                                          ║     │
│  ║                                                                       ║     │
│  ║   OpenClaw (gateway) ──► MetaMCP ──► Qwen 3.6 27B (LM Studio)        ║     │
│  ║     ▲                      │                                          ║     │
│  ║     │                      ├──► tool MCPs (Tier 3)                    ║     │
│  ║     │                      │                                          ║     │
│  ║     │                      └──► TOOLS.md orchestrator                 ║     │
│  ║     │                              └──► tools/*.md (loaded on demand) ║     │
│  ║                                                                       ║     │
│  ║   • Telegram   → @YourSentinelBot (main agent)                       ║     │
│  ║                  @YourWatchdogBot (watchdog)              ║     │
│  ║                  @azsmdl_bot (SMDL standalone)                       ║     │
│  ║   • Mini-app   → your-domain.example.com (TOTP-gated web UI)        ║     │
│  ╚══════════════════════════════════════════════════════════════════════╝     │
│                                                                               │
│  ╔══════════════════════════════════════════════════════════════════════╗     │
│  ║  TIER 2 — Gaming  (domain integration)                                ║     │
│  ║                                                                       ║     │
│  ║   • ARK Survival Evolved server (rcon-manager, dedicated)            ║     │
│  ║   • Crib Watchdog — power monitor + Steam session tracker            ║     │
│  ║                     (separate repo: YOUR_GITHUB_USERNAME/crib-watchdog)          ║     │
│  ╚══════════════════════════════════════════════════════════════════════╝     │
│                                                                               │
│  ╔══════════════════════════════════════════════════════════════════════╗     │
│  ║  TIER 3 — Standalone  (deployable on their own; AI consumes via MCP) ║     │
│  ║                                                                       ║     │
│  ║   MEDIA                                                               ║     │
│  ║   ├─ SMDL  (sentinel-smdl)              🟢 PUBLIC @ YOUR_GITHUB_USERNAME          ║     │
│  ║   │   GitHub: github.com/YOUR_GITHUB_USERNAME/sentinel-smdl  v1.0.0              ║     │
│  ║   │   GHCR:   ghcr.io/YOUR_GITHUB_USERNAME/sentinel-smdl     v1.0.0 (amd64+arm64)║     │
│  ║   │   Bot:    @azsmdl_bot                                             ║     │
│  ║   │   Engine: yt-dlp + gallery-dl + RecorderBridge + plugin tier     ║     │
│  ║   │                                                                   ║     │
│  ║   └─ SMDL MCP  (ytdlp-mcp/)            🟡 v1.0.0 parity, internal   ║     │
│  ║       12 MCP tools incl. 4 live-recording (record_live_*)            ║     │
│  ║       Same RecorderBridge engine; agent-facing surface               ║     │
│  ║                                                                       ║     │
│  ║   COMMS                                                               ║     │
│  ║   ├─ Google Workspace MCP    Gmail / Calendar / Drive                 ║     │
│  ║   ├─ Translate MCP           LibreTranslate wrapper (en/zh-Hans/ru)   ║     │
│  ║   ├─ OneDrive MCP            personal OneDrive file access            ║     │
│  ║   └─ GitHub MCP              repos/issues/PRs/Actions                 ║     │
│  ║                                                                       ║     │
│  ║   AGENT INFRA                                                         ║     │
│  ║   ├─ MetaMCP   (ghcr.io/YOUR_GITHUB_USERNAME/metamcp:v2.4.22-sentinel-20260510)  ║     │
│  ║   │     forked from metatool-ai/metamcp (PR#283+#273 cherry-picked)  ║     │
│  ║   ├─ Reminders MCP    APScheduler-backed                             ║     │
│  ║   ├─ Memory MCP       mcp-memory-service (SQLite-vec)                ║     │
│  ║   ├─ Maps MCP         Google Maps tools                              ║     │
│  ║   ├─ Playwright MCP   browser automation (CDP-attached Chromium)     ║     │
│  ║   ├─ Tavily MCP       web search                                     ║     │
│  ║   └─ fetch / sqlite / git / brave-search   (stdio MCPs)             ║     │
│  ║                                                                       ║     │
│  ║   VPN / TUNNELING                                                     ║     │
│  ║   ├─ Tailscale         user mesh (host on tailnet 100.73.83.20)      ║     │
│  ║   ├─ Headscale         sovereign control plane (deployed, parked)    ║     │
│  ║   ├─ AmneziaWG         RKN-bypass tunnel for outsider (UDP 51234)   ║     │
│  ║   └─ Cloudflare Tunnel public ingress for your-domain.example.com domains    ║     │
│  ║                                                                       ║     │
│  ║   SECURITY / OPS                                                      ║     │
│  ║   ├─ Vaultwarden      127.0.0.1:8085 — self-hosted password mgr     ║     │
│  ║   ├─ Forgejo          127.0.0.1:3000 — private git ("secret journal")║     │
│  ║   ├─ Sentinel Watchdog system service — power events, GitHub sync   ║     │
│  ║   └─ Crib Watchdog    Home Assistant power-spike monitor             ║     │
│  ║                                                                       ║     │
│  ║   QUALITY OF LIFE                                                     ║     │
│  ║   ├─ Firefly III      personal finance (profile: finance)            ║     │
│  ║   └─ Inference Bridge HTTP proxy for LM Studio (spike classifier)    ║     │
│  ╚══════════════════════════════════════════════════════════════════════╝     │
│                                                                               │
└──────────────────────────────────────────────────────────────────────────────┘

LEGEND:
  🟢 = carved out into public GitHub repo
  🟡 = ready / clean; not yet carved out
  ⚪ = scope only / future work
```

---

## 2. Deployment topology — what's actually running

```
                    Windows 11 Pro host (sentinel-host.tail00dd59.ts.net)
   ┌──────────────────────────────────────────────────────────────────────────┐
   │                                                                          │
   │   ┌──────────────────────────────┐    ┌────────────────────────────────┐ │
   │   │   WSL2 / Ubuntu-24.04        │    │   Docker Desktop / docker-      │ │
   │   │                              │    │   compose stack                  │ │
   │   │   • OpenClaw gateway         │    │                                  │ │
   │   │     (systemd, port 18789)    │    │   • metamcp (app)                │ │
   │   │   • LM Studio (host bridged) │◄──►│       127.0.0.1:12008            │ │
   │   │   • TOOLS.md orchestrator    │    │   • postgres                     │ │
   │   │     + tools/*.md sub-files   │    │   • ytdlp-mcp (SMDL MCP)         │ │
   │   │   • Telegram channel         │    │       127.0.0.1:8088             │ │
   │   │                              │    │   • smdl (SMDL standalone)       │ │
   │   └──────────────────────────────┘    │       127.0.0.1:8096             │ │
   │                                       │   • reminders-mcp                │ │
   │   ┌──────────────────────────────┐    │   • google-workspace-mcp         │ │
   │   │   Native Windows processes   │    │   • maps-mcp                     │ │
   │   │                              │    │   • memory-mcp                   │ │
   │   │   • pythonw watchdog.py      │    │   • github-mcp                   │ │
   │   │     (Sentinel Watchdog)      │    │   • onedrive-mcp                 │ │
   │   │   • pythonw bridge.py        │    │   • translate-mcp + libretrans   │ │
   │   │     (sentinel-miniapp-v2)    │    │   • vaultwarden                  │ │
   │   │   • LM Studio (qwen3.6-27b)  │    │   • headscale (parked)           │ │
   │   │     port 1234, GENERATING    │    │   • amneziawg (UDP 51234)        │ │
   │   │   • infer-bridge.py          │    │   • forgejo (profile: journal)   │ │
   │   │     port 8095 spike proxy    │    │       127.0.0.1:3000             │ │
   │   │                              │    │   • firefly + firefly-db         │ │
   │   └──────────────────────────────┘    │     (profile: finance)           │ │
   │                                       │   • smdl-test (WSL2, on demand)  │ │
   │   ┌──────────────────────────────┐    │     for fresh-install tests      │ │
   │   │   External                   │    │                                  │ │
   │   │                              │    └────────────────────────────────┘ │
   │   │   • G:\YT-DLP  (media root)  │                                       │
   │   │   • OneDrive (host-mounted)  │                                       │
   │   │   • Home Assistant (LAN)     │                                       │
   │   │   • Steam (host)             │                                       │
   │   └──────────────────────────────┘                                       │
   │                                                                          │
   └──────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Data flow — a user message hits the stack

```
   Telegram user                 OpenClaw                MetaMCP                LM Studio
       │                            │                       │                       │
       │  "summarise this PDF"      │                       │                       │
       ├───────────────────────────►│                       │                       │
       │                            │ load TOOLS.md         │                       │
       │                            │ load tools/onedrive.md│                       │
       │                            │ (orchestrator pattern)│                       │
       │                            │                       │                       │
       │                            ├──── model_call ──────────────────────────────►│
       │                            │                                               │
       │                            │◄─── tool_call: onedrive_read ─────────────────│
       │                            │     (via Qwen 3.6 27B reasoning)              │
       │                            │                       │                       │
       │                            ├──── route ───────────►│                       │
       │                            │                       │                       │
       │                            │                       ├──► onedrive-mcp      │
       │                            │                       │     (HTTP, port 8087)│
       │                            │                       │                       │
       │                            │                       │◄── PDF content ──────│
       │                            │◄──────────────────────│                       │
       │                            │                                               │
       │                            ├──── model_call (with PDF text) ──────────────►│
       │                            │                                               │
       │                            │◄─── summary tokens ───────────────────────────│
       │                            │                                               │
       │◄────────── reply ──────────│                                               │
       │  "Summary: ..."            │                                               │
       │                            │                                               │
   (memory_store after if          (Watchdog observes      (Source-of-Truth        (Spike-classifier
    summary is "remember this")     stalled sessions)       discipline applies)     in infer-bridge)
```

---

## 4. Network boundaries

```
                                   PUBLIC INTERNET
                                         │
                                         ▼
                  ┌─────────────────────────────────────────────┐
                  │   Cloudflare Tunnel — *.your-domain.example.com     │
                  │   sentinel.   → mini-app                    │
                  │   media.      → SMDL file delivery          │
                  │   headscale.  → control plane (parked)      │
                  │   reminders.  → reminders MCP (optional)    │
                  └─────────────────────────────────────────────┘
                                         │
                                         ▼
                  ┌─────────────────────────────────────────────┐
                  │   Tailnet — sentinel-host.tail00dd59.ts.net │
                  │   100.73.83.20  host                        │
                  │   100.85.233.10 phone                       │
                  │   Friend access via AmneziaWG → port 51234  │
                  │   Tailscale serve --tcp 8096 → SMDL files   │
                  └─────────────────────────────────────────────┘
                                         │
                                         ▼
                  ┌─────────────────────────────────────────────┐
                  │   127.0.0.1 (loopback, host-local only)     │
                  │   3000  Forgejo (private git, journal tier) │
                  │   5050  libretranslate                       │
                  │   8085  Vaultwarden                          │
                  │   8087  onedrive-mcp                         │
                  │   8088  ytdlp-mcp (SMDL MCP)                 │
                  │   8089  reminders-mcp                        │
                  │   8090  google-workspace-mcp                 │
                  │   8091  github-mcp                           │
                  │   8092  maps-mcp                             │
                  │   8093  onedrive-mcp web auth                │
                  │   8094  translate-mcp wrapper                │
                  │   8095  infer-bridge (LM Studio proxy)       │
                  │   8096  smdl (SMDL standalone)               │
                  │   8099  headscale                            │
                  │   8180  firefly (profile: finance)           │
                  │   9090  prometheus / metrics                 │
                  │   12008 metamcp                              │
                  │   18789 openclaw-gateway (WSL)               │
                  │   18791 openclaw browser-control             │
                  │                                              │
                  │   4xxx  test/dev ports (fresh-install, etc.) │
                  └─────────────────────────────────────────────┘
```

---

## 5. Future GitHub org layout (target)

```
sentinel-* (GitHub org — auto-created when ≥3 components reach 🟢)
│
├── sentinel-stack-public   ✅ exists — slimmed monorepo, integration glue
├── sentinel-smdl           ✅ exists — v1.0.0 + GHCR image, public 2026-05-11
├── sentinel-watchdog       ✅ exists — already its own private repo
│
├── sentinel-ai             🟡 cleanup needed (OpenClaw config templates,
│                              mini-app, bridge.py needs chat-ID extraction)
├── sentinel-vpn            🟡 templates ready (Headscale + AmneziaWG configs,
│                              install walkthrough from proposals/*)
├── sentinel-email          🟡 google-workspace-mcp already self-contained,
│                              needs README + license clarification
├── sentinel-metamcp-fork   ⚪ already forked + GHCR-published; not yet
│                              promoted as a sentinel-org public repo
└── sentinel-finance        ⚪ not built yet (Firefly + statement parser scope)
```

---

*Drift between this doc and reality: when a major change ships (new service deployed, repo carved out, MCP added), update this file and bump the date at the top.*
