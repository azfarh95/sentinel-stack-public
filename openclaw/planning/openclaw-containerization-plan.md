# OpenClaw containerization — phased CLONE-FIRST plan (no cutover until proven)

**Date:** 2026-06-17 · **Status:** Phase 1 DONE + Phase 2 gate-1 DONE · **Decision:** owner chose
"clone first, cutover only once stable."

**PROGRESS (2026-06-17):** ✅ Phase 1 — image `openclaw-shadow:2026.6.1` (clean `npm i -g`, public
registry), shadow gateway **Up** on `:18790` (HTTP 200), channel-free config (no live-bot conflict),
Qwen via `host.docker.internal:8095`; live Dove canary `ok` side-by-side (zero impact). ✅ Phase 2
gate-1 — a containerized `docker exec … agent --session-id … --json` turn **succeeded**
(`winnerModel: qwen/qwen3.6-27b, result: success, reply "OK"`) — OpenClaw runs real turns against
Qwen entirely in Docker, no Ubuntu-24.04 dependency. Build/config artifacts: `docker-shadow/`
(Dockerfile + compose + seed/, seed gitignored — holds a local api-key). Config gotchas found:
`gateway.bind` takes keywords (`auto` in a container) + binding non-loopback needs
`OPENCLAW_GATEWAY_TOKEN`. ✅ Phase 2 gate-2 — wired `mcp.servers.metamcp` (`http://metamcp:12008/...`, shadow joined
`metamcp-local_metamcp-network`); a containerized turn **invoked a metamcp tool**
(`toolSummary: {calls:4, tools:[tool_search, tool_call, metamcp__filesystem__list_directory],
failures:0}`). FINDING: a *fresh* MCP connect times out (`-32001`) when metamcp's `default`
namespace has DEAD downstream members — stopping MCP containers for RAM (sentinel-home-mcp,
portfolio-mcp) degrades metamcp's namespace for any fresh OpenClaw connect (live Dove survived on a
cached session; a Dove *restart* / the auto-recovery would hit it too). Restarting the members fixed
it (tool catalog 28→202). Multi-step tool turns are slow on the shared GPU (use the 600 s prod
timeout, not 120 s).
**NEXT:** Phase 3 — soak + the full tool/skill/**file-delivery** matrix from a container + a
WSL-vs-container parity harness + timeout tuning.

## Goal
Run OpenClaw as a **Docker container in the `docker-desktop` distro** (the resilient,
Docker-managed WSL2 distro that stayed healthy through every `E_UNEXPECTED` wedge), removing
Dove's dependency on the **fragile `Ubuntu-24.04` distro** and the **per-turn `wsl -d
Ubuntu-24.04` agent spawn** (the exact path that throws `Wsl/Service/E_UNEXPECTED`).

**This does NOT replace the RAM upgrade** — the container still lives in the same 12 GB WSL2 VM,
so memory pressure persists. It removes the *Ubuntu-spawn failure mode* + gives Docker-managed
recovery. Pair with RAM; don't treat as a substitute.

## Current topology (what moves)
- **Windows:** `brain_wrapper.py` (Python, in the bot + bridges) orchestrates a turn: persist to
  brain_store (postgres `metamcp-pg`), load history, **spawn a one-shot `node openclaw agent`
  per turn via `wsl -d Ubuntu-24.04`**, parse the JSON. (Host-wide mutex
  `Local\SentinelOpenClawGatewayTurn` serialises against the persistent gateway.)
- **WSL Ubuntu-24.04:** `node …/openclaw/dist/index.js gateway --port 18789` (systemd
  `openclaw-gateway.service`) + the per-turn one-shot agent + `~/.openclaw` config/state.
- **docker-desktop:** `metamcp`, `metamcp-pg` (= brain_store + metamcp DB), the rest of the stack.
- **Windows:** Qwen `:8095`/`:1234`.

Key facts: OpenClaw is an **npm-global node app** (`openclaw@2026.6.1`, `dist/index.js`) that
**already has a `gateway` server mode** (`--port 18789`). The per-turn agent is **stateless**
(fresh session-id; brain_store is the only history of record) → it needs only **Qwen + metamcp +
`~/.openclaw`**, NOT brain_store.

## Guiding principles
- **Clone, don't cutover.** The live WSL gateway + the live `brain_wrapper` wsl-spawn path stay
  UNTOUCHED until Phase 4. Every phase reversible.
- **Isolation:** the shadow uses its OWN `~/.openclaw` copy + TEST session-ids; it must NOT touch
  the live brain_store, the live session state, or grab the host turnstile mutex. Validation uses
  SAFE prompts first (no side-effecting tools) — the shadow can reach real metamcp tools.
- **Mind the memory:** build/run in `docker-desktop` (resilient); stop the shadow when idle; each
  build step health-gates the LIVE Dove canary (no regression).

---

## Phase 1 — Build the image + run the shadow gateway (the clone)
**Work:**
- Provenance: clone the EXACT installed package (copy `/home/azfar/.npm-global/lib/node_modules/
  openclaw` out of WSL — version-exact, avoids registry ambiguity since "openclaw" may be
  private/custom) into the build context; node base image matching the WSL node major.
- `Dockerfile`: node base + the copied package + entrypoint `node /opt/openclaw/dist/index.js
  gateway --port 18789`.
- Compose service `openclaw-shadow` (own profile — NOT in the default `up`; host port
  **18790→18789** so it can't collide with the live `:18789`; own `.openclaw` volume seeded from
  a COPY of the live config with the model `baseUrl` repointed to `host.docker.internal:8095`).
**GATE:**
- [ ] Container starts; `openclaw gateway` boots; responds on `:18790` (health/version).
- [ ] LIVE Dove canary still `status:ok` (zero impact).

## Phase 2 — Prove an agent turn inside the container
**Work:**
- `docker exec openclaw-shadow node …/index.js agent --json --message "Reply with: OK"` (TEST
  session) → `status:ok`, reaching Qwen.
- Then a tool-using prompt (a READ-ONLY tool first) → confirms metamcp connectivity from the
  container.
- Parity battery vs the WSL one-shot (read/extract, a tool call, a multi-step).
**GATE:**
- [ ] ≥ N turns `status:ok` with correct tool behaviour from the container.
- [ ] No interference: live Dove canary stays `ok` while shadow turns run.

## Phase 3 — Soak + stability + the tool/skill/file-delivery matrix (the long pole)
**Work:**
- Sustained load + over-time soak; confirm `restart:always` + healthcheck recover a killed
  process; confirm it rides memory pressure better than the Ubuntu spawn.
- **Validate every tool/skill/file-delivery path from inside a container** — anything assuming the
  WSL filesystem / host paths gets remapped. This is where the real time goes.
- A parity harness: same prompt → shadow vs WSL → diff; track divergences.
**GATE:**
- [ ] Stable over a soak window; parity acceptable; the tool/skill/file matrix all green.

## Phase 4 — Cutover (DEFERRED; only on Phase-3 confidence)
**Work:**
- Point `brain_wrapper`'s spawn at the container (`docker exec openclaw …`) instead of `wsl -d
  Ubuntu-24.04`; rework the AI-012 out-of-band temp-file transport for `docker exec`; behind a
  **feature flag** to flip back instantly.
- Verify every surface (bot, bridges, crons) + the session turnstile.
- Keep the WSL gateway as rollback; retire it (+ the Ubuntu-distro dependency, +
  `SentinelGatewayAutoRecover`) only after a green soak.
**GATE:** full surface matrix green; rollback (flip the flag) verified.

---

## Risks
- **Memory** — still the shared 12 GB VM. Pair with RAM. (Container degrades more gracefully than
  a cold distro spawn, but a whole-VM starve still hurts.)
- **Provenance** — copy-from-WSL for an exact clone (don't trust `npm i openclaw` to be the same
  package).
- **Tool/skill/file-delivery WSL assumptions** — Phase 3 long pole; the most likely source of
  "works in WSL, breaks in container."
- **Qwen reachability** — `host.docker.internal:8095` (the known host-IPv4 pin gotcha).
- **Side-effecting tools** — validate with safe/read-only prompts before anything that sends/writes.
