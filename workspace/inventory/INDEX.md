# Sentinel Stack — Inventory Index

This directory is the **single entry point** for understanding what's running, what depends on what, and what needs fixing. Read this file first; drill into the YAMLs as needed.

**Goal**: stop re-running the same surveys (`docker ps`, `lms ps`, hardcoded-path greps) every conversation. Maintain it like a tidy workshop — a place for every screw and every screw in its place.

---

## Files in this directory

| File | Role | Maintenance |
|---|---|---|
| `INDEX.md` | This file. Human-readable big picture + pointers. | Hand-edited as architecture changes. |
| `architecture.yaml` | Component groups, descriptions, dependency edges, open decisions. The "why" behind each piece. | Hand-edited — bump `last_reviewed` on changes. |
| `running.yaml` | Live state: docker containers, LM models, processes, ports, scheduled tasks. | **Auto-generated** by `scripts/refresh_inventory.py`. Don't hand-edit. |
| `violations.yaml` | Hardcoded paths/identifiers tracker — open / fixed / acceptable. | Hand-edited as fixes land. |

---

## How to use this

**Quick "what's running" check:**
```powershell
python scripts\refresh_inventory.py    # ~10s, refreshes running.yaml
```
Then read `running.yaml`.

**"What's the architecture?" question:**
Read `architecture.yaml` — top-down (groups → components → dependencies).

**"What needs fixing for V6?" question:**
Read `violations.yaml`. Focus on `phases.A` and `phases.C` (highest-leverage).

**"What's the link between X and Y?":**
`architecture.yaml → dependencies` lists every hard/soft edge.

---

## Quick reference card

### Component count (target = simpler)

| Group | Containers | In V6? |
|---|---|---|
| metamcp_core | 12 | ✅ |
| sentinel_native (processes) | 5 | ✅ |
| sentinel_browser (processes) | 3 | ✅ |
| crib_watchdog | 8 containers | ❌ separate repo |
| claude_assistant | 2 | ❌ V4 standalone |
| game_server | 2 | ❌ unrelated |

V6 scope: **~17 components** (~12 docker + 5 native + 1 WSL service). Down from 37 by extracting the out-of-scope groups.

### Critical port map

```
1234     LM Studio (native)
8095     infer_bridge (proxy → LM Studio)
8098     bridge.py (Sentinel mini-app)
8931     Playwright MCP
9222     Chrome CDP (V3 browser panel)
12008    MetaMCP
18789    OpenClaw gateway (WSL)
8087-8094  individual MCP servers
```

### LM Studio settings to remember

- Primary: `qwen/qwen3.6-27b` @ **98304 context** (32K was too small — system prompt is ~36K)
- Eval batch size: **512** (faster prefill)
- Unified KV Cache: **on**
- Parallel slots: **4**

### Open V6 decisions (in `architecture.yaml → open_decisions`)

1. **V6 audience**: me-only or community? Drives installer UX, license audit, secret handling.
2. **Compose consolidation**: merge 6 compose files → 2 with profiles? (1 hour, big payoff)
3. **OpenClaw in Docker**: drop WSL2 dep? (deferred to V6.x)
4. **Auto-update strategy**: winget repo / in-app notify / reinstall?

### V6 prep status (from `violations.yaml`)

- **73 hardcoded violations** across 22 files.
- **57 hard** must fix, **14 soft** should fix, **2 acceptable**.
- **0 fixed so far** — Phase A (paths module) is the first move.

---

## When this file goes stale

`architecture.yaml` carries a `last_reviewed` date. If older than 30 days, do a quick review:
1. Run `refresh_inventory.py`, diff `running.yaml` against last commit.
2. Anything new? Add to `architecture.yaml` under the right group.
3. Anything gone? Mark with `removed: <date>` rather than deleting.
4. Bump `last_reviewed`.

Same drill for `violations.yaml` after every cleanup commit — update status fields, archive closed items.

---

## Conventions

- **Severity**: `hard` = portability blocker; `soft` = should be config; `acceptable` = stays literal.
- **Phase**: A (paths module) → B (script-relative refs) → C (drop fallback defaults) → D (env-var parameterization) → E (fresh-user test).
- **Status**: `open` / `in_progress` / `fixed_in:<sha>` / `wont_fix:<reason>`.

Don't overthink the schema — these YAMLs are notes for future-you (and future Claude sessions). Clarity beats completeness.
