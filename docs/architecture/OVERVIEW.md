# Project Sentinel — Overview

**Goal**: a homelab + life-automation stack with commercial-shareable components.
**Strategy**: monorepo while components mature; split into a `sentinel-*` GitHub org once each is independently deployable + documented + licensable.

This file is the **forward-looking taxonomy**. For where each thing currently lives in the codebase, see [`COMPONENT-MAP.md`](./COMPONENT-MAP.md).

---

## Tree

```
Project Sentinel
│
├── AI                          ← agent core (the "brain")
│   • OpenClaw + MetaMCP + Qwen 3.6 27B
│   • Mini-app, watchdog bot, Telegram interface
│   • Memory MCP, Tool MCPs
│
├── Gaming                      ← gaming-life integrations
│   • ARK Survival Evolved server
│   • Crib Watchdog (power spike + Steam session tracking)
│
└── Standalone                  ← deployable on their own; useful without the AI
    │
    ├── Home Automation
    │   • Home Assistant integration
    │   • Crib Watchdog spike monitor (LM Studio inference detection,
    │     gaming session classifier)
    │
    ├── Media
    │   └── Downloader
    │       • SMDL (s.) — standalone media downloader (yt-dlp + gallery-dl)
    │       • SMDL MCP — MCP-wrapped variant for Sentinel agent calls
    │       • Sibling implementations; standalone is the dev-first surface
    │
    ├── VPN
    │   • Tailscale-on-stack — phone access to home services
    │   • Headscale — sovereign control plane (deployed, currently unused)
    │   • AmneziaWG — RKN-bypass tunnel for outsider access
    │
    └── Quality-of-life tools
        • Finance — Firefly III + bank statement parser (V2 scope)
        • Email management — Gmail labels + filters + bank-statement aggregation
        • Calendar management — Google Workspace MCP
        • Health & lifestyle monitoring — TBD
        • Calories tracker — TBD
```

---

## Layered model

The three top-level categories aren't peers in capability — they're tiers in a brain-and-tools architecture:

| Tier | Category | Role | Depends on |
|---|---|---|---|
| 1 | AI | Reasoning, routing, synthesis | Standalone tools to act through |
| 2 | Gaming | Domain integration | (largely independent — measures + manages gaming life) |
| 3 | Standalone | Tools the AI uses, AND tools usable on their own | (independent by design) |

A **Standalone** component is "carve-out ready" when:
- ✅ Self-contained Dockerfile / install steps
- ✅ Own README that doesn't reference the wider stack
- ✅ Permissive license declared (or explicitly private)
- ✅ Clean dependency boundary (doesn't reach into other components' code)
- ✅ Deployable by a stranger from a fresh clone

Once a component hits all five, it's a candidate for splitting into its own repo under the future `sentinel-*` GitHub org via `git subtree split`.

---

## Future GitHub org layout (target)

When the org is created (TBD trigger: ≥3 components carve-out-ready):

```
sentinel-* (GitHub organization)
│
├── sentinel-stack              ← integration repo (this monorepo, slimmed)
│                                  pulls components in as submodules / docker-compose deps
│
├── sentinel-ai                 ← OpenClaw config templates, MetaMCP, mini-app
├── sentinel-smdl               ← standalone media downloader (carve-out candidate #1)
├── sentinel-vpn                ← Tailscale recipes + AmneziaWG templates + Headscale config
├── sentinel-watchdog           ← power spike + gaming session monitor
├── sentinel-finance            ← Firefly + bank statement parser
└── sentinel-email              ← Gmail label/filter MCP + parser tooling
```

Naming convention: `sentinel-<single-word>` for promotability. Each repo gets its own:
- `README.md` (self-contained marketing + install)
- `LICENSE` (likely MIT for community-friendly components)
- `CHANGELOG.md`
- `docker-compose.yml` (for stack-as-deployed style components)
- GitHub Actions for build/test where applicable

---

## Versioning convention (per Standalone component)

Each Standalone component progresses through the same 4-stage maturity ladder:

| Stage | What | Marker of done |
|---|---|---|
| **V1 — Discovery + Docker** | Functional component, runs in Docker, owner-only use, debugging the long tail | 1-2 months of own daily use without major regressions |
| **V2 — UX + mini-app** | Dedicated TOTP-gated web mini-app for managing the component (history, status, manual triggers); CLI/Telegram UX trimmed | Mini-app reachable via tailnet, parity with Telegram-bot UX |
| **V3 — Native binary** | PyInstaller / Nuitka builds for Windows / Linux / macOS published in GitHub Releases | Fresh-VPS install from binary works, no Docker required |
| **V4 — MSI / installer** | Inno Setup (Win) / pkg (Mac) / deb (Linux) wrapping V3, with service registration + uninstaller | Non-technical user can install via wizard and reach a working state |

Convention: each component has a status table in its own README mapping itself onto this ladder. Don't skip stages — V2 polish before V1 stability is wasted work; V3 packaging before V2 UX is shipping a polished broken thing.

## Mini-app per Standalone component (architectural pattern)

Each Standalone product carries its own mini-app — a TOTP-gated web UI for managing that one component, deployed under its own subdomain or path:

```
your-domain.example.com       ← OpenClaw / Sentinel AI mini-app (sentinel-miniapp-v2)
                                  Built first; the prototype the others learn from.

smdl.your-domain.example.com           ← SMDL mini-app (V2 scope, not yet built)
                                  Recording history, /m/ delivery shortcuts, retry budget reset,
                                  manual /stop, per-platform cookie management.

rcon.your-domain.example.com           ← RCON gaming mini-app (future, Gaming tier)
                                  ARK server console, player kick/ban, save management,
                                  scheduled restarts.
```

These mini-apps will eventually share a common framework (Flask + Telegram WebApp SDK + TOTP gate + service-status panel) — extracted once we have ≥2 working mini-apps to abstract from. For now, OpenClaw's mini-app is the only reference; SMDL's mini-app inherits its patterns, and the third mini-app triggers framework extraction.

**Shared framework lives at**: `sentinel-miniapp-framework` (future repo) once carve-out happens. Until then, copy-paste from OpenClaw's mini-app and document the divergences.

## Iteration model

> "We update monorepos by monorepos until we're ready for integration."

Translation: each component-directory inside `sentinel-stack/` is treated as if it were its own monorepo. PRs touch one component at a time. When a component reaches carve-out maturity, `git subtree split --prefix=<component-path>` extracts the history cleanly into a new repo.

This avoids:
- Premature splitting (each split adds maintenance overhead)
- Refactor paralysis (component design evolves while you build)
- Cross-component churn (changes in one component don't dirty PRs in another)

---

## Status snapshot — 10 May 2026

| Component | Carve-out maturity | Comments |
|---|---|---|
| AI / OpenClaw config | 🟡 Documented but config is personal | Will need template-ization (strip secrets, generic SOUL.md) before public |
| AI / MetaMCP | ❌ Forked stale upstream — needs decision | See `2026-05-10-Headscale-Phase1.md` MetaMCP investigation |
| AI / Mini-app | 🟡 Works, needs install docs | TOTP + secrets management baked in |
| Gaming / ARK | ❌ Personal config, not extracted | Out of scope for now |
| Gaming / Crib Watchdog | 🟡 Self-contained code, personal config | Splittable with cleanup |
| Standalone / SMDL (s.) | 🟢 **V1 active — closest to carve-out ready** | Self-contained, has Dockerfile, has scope doc, has tests in flight. V2 (mini-app) scoped but not started. |
| Standalone / SMDL MCP | 🟡 Needs sync from standalone | Per `feedback_smdl_dev_workflow.md` |
| Standalone / VPN — Headscale | 🟢 Templates ready; data is gitignored | `headscale-config/` is generic |
| Standalone / VPN — AmneziaWG | 🟢 Templates ready; data is gitignored | `amneziawg-config/` is generic |
| Standalone / VPN — Tailscale | 🟡 Recipes, not yet documented | Setup walkthrough exists in proposals/ |
| Standalone / Email | 🟢 google-workspace-mcp is self-contained | Built today, label + filter tools |
| Standalone / Finance | ⚪ V2 scope only | Statement parser not built yet |

Legend: 🟢 ready / 🟡 cleanup needed / ❌ not yet / ⚪ design only

---

## Why this taxonomy matters

It separates **personal mess** (your specific Gmail account, your specific OpenRouter key, your home Wi-Fi SSID) from **reusable patterns** (the SMDL downloader, the AmneziaWG config templates, the Gmail label MCP tools). The reusable patterns are what could be commercial-shareable; the personal mess stays in a private overlay.

When you're ready, the carve-out converts a component from "Azfar's personal SMDL setup" → "an open-source Sentinel-branded media downloader that anyone can run." Same code, different framing, different licensing.
