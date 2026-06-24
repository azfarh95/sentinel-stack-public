# Component Map — current paths → future repos

Translation table between today's monorepo layout and the target `sentinel-*` GitHub org.

When a component gets carved out, the procedure is:
```
git subtree split --prefix=<current-path> -b split-<component>
git push <new-repo-url> split-<component>:main
```
History preserved, monorepo unaffected (component stays as a submodule or just a doc reference after split).

---

## Mapping

| Future repo | Current path(s) in `sentinel-stack` | Maturity | Carve-out blockers |
|---|---|---|---|
| **`sentinel-smdl`** | `smdl/` | 🟢 Carve-out candidate #1 | None blocking. Needs: own README, LICENSE choice (MIT), strip user-specific config from `smdl.json` example |
| **`sentinel-ai`** | `sentinel-miniapp-v2/`, OpenClaw config templates (currently in `\\wsl.localhost\.openclaw\workspace\`) | 🟡 Cleanup needed | Personal SOUL.md / TOOLS.md content; `bridge.py` has hardcoded chat IDs |
| **`sentinel-vpn`** | `headscale-config/` (templates), VPN scope docs in `workspace/proposals/2026-05-10-Headscale-*.md`, `2026-05-10-AmneziaWG-Phase3.md` | 🟢 Templates already generic | Live data already gitignored. Need: assembly README walking through Tailscale/Headscale/AmneziaWG choice tree |
| **`sentinel-watchdog`** | `watchdog/` | 🟡 Cleanup needed | Hardcoded chat IDs, secrets paths assume WCM |
| **`sentinel-email`** | `google-workspace-mcp/` | 🟢 Already self-contained MCP | Need: own README, license clarification (we built on top of MCP SDK) |
| **`sentinel-finance`** | (not built yet) `workspace/proposals/2026-05-10-Statement-Automation/` is the design | ⚪ Scope only | Wait for V1 implementation |
| **`sentinel-stack`** (slimmed) | this repo, after carving the rest out | — | Becomes integration glue: `docker-compose.yml` referencing components as submodules / pinned images |
| **`sentinel-metamcp-fork`** | (would fork from `metatool-ai/metamcp` per the raid scope MetaMCP investigation finding) | ⚪ Decision pending | Either carry our own patched fork or stay on upstream stale; see `2026-05-10-Headscale-Phase1.md` MetaMCP section |
| **`sentinel-gaming`** | `crib-watchdog/` (sibling repo `YOUR_GITHUB_USERNAME/crib-watchdog` already exists) — and ARK personal config | 🟡 Already partly split | Crib watchdog already its own repo; ARK personal config not promotable |

---

## Decision: when does each component get carved out?

Default: **stay in monorepo**. The trigger to carve out is reaching carve-out maturity (5 boxes ticked from `OVERVIEW.md`) AND having a concrete *external* reason to share it (someone wants to use it, you want to write about it publicly, you want to license it).

Premature carve-outs add maintenance cost without benefit. Late carve-outs are fine — `git subtree split` works on any commit history.

---

## Working order — what to clean up next, in priority

If you want to make a single component "promotable" before any carve-out:

1. **`sentinel-smdl`** — closest to ready. The cleanup work (README, LICENSE, strip personal config from `smdl.json`) is ~3-4 hours. Demonstrates the workflow.
2. **`sentinel-vpn`** — second-closest. Templates already exist; the carve-out is mostly assembling the proposals/* docs into a coherent install guide.
3. **`sentinel-email`** — would need a generic-name rebrand (today it's `google-workspace-mcp` which is descriptive but not Sentinel-branded).

After these three are sharable, the org-creation trigger fires (≥3 carve-out-ready components) and we make the actual GitHub org.

---

## Don't carve out (stay in `sentinel-stack`)

These are too entangled / too personal / too small to justify separate repos:

- Per-user secrets sync scripts (`scripts/sync_env_from_wcm.ps1`) — Windows-WCM-specific
- Personal benchmark logs (`workspace/benchmarks/*`)
- Per-deployment notes, docs related to azfar's specific home
- Anything in `workspace/Perplexity reviews/` — contextual, not reusable
