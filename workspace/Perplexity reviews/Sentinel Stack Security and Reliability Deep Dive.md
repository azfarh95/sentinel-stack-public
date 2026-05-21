# Sentinel Stack Security and Reliability Deep Dive

## Executive summary
Sentinel Stack is a sophisticated local-first AI assistant architecture built around OpenClaw, MetaMCP, LM Studio, and multiple MCP servers, orchestrated via Docker and Windows tooling. The system provides extensive capabilities (calendar, file storage, GitHub, translation, maps, reminders, yt-dlp, OneDrive) and a Telegram-based control plane, which collectively introduces meaningful security, reliability, and operational risks if not rigorously hardened.

The most critical issues identified are: (1) unauthenticated memory access and permissive CORS in the Sentinel bridge, (2) mismatch between documentation and actual network binding, (3) insecure default credentials in Docker configuration, and (4) fragile concurrency and routing logic in the inference bridge. Addressing these weaknesses requires tightening trust boundaries, enforcing authentication on internal HTTP services, strengthening configuration hygiene, and adopting more robust process supervision and observability.

## System architecture overview

### High-level architecture
The README describes a two-bot architecture: a Sentinel bot (AI assistant) and a Watchdog bot (management plane), both on Telegram. The AI assistant is powered by OpenClaw running inside WSL2 on Ubuntu 24.04, which communicates with MetaMCP (an MCP aggregation gateway) running in Docker and orchestrating multiple tool-specific MCP servers plus a PostgreSQL database.

MetaMCP exposes tools for Memory, Reminders, Google Workspace, Maps, GitHub, OneDrive, yt-dlp, and Translate, backed by services such as LibreTranslate and LM Studio for local model inference. LM Studio is accessed via an inference bridge on port 8095 that classifies prompts and dynamically rewrites the target model before forwarding requests to LM Studio at 127.0.0.1:1234.

The Watchdog component monitors services, exposes an HTTP status server, and bridges to a Telegram Mini App through a dedicated bridge and Cloudflare Tunnel. A separate Sentinel bridge process serves a dashboard and proxies to Memory MCP and other local services for status and introspection, returning JSON data to the front-end.

### Deployment and configuration model
The root compose file `docker-compose.yml` defines at least two core services: `app` (MetaMCP) and `postgres` (PostgreSQL), plus a named volume and a bridge network. Environment variables are loaded from `.env`, with additional defaults set in the `environment` section for database connection details, app URLs, auth secrets, and a flag to transform localhost references inside Docker.

The repository also contains multiple alternative or supplemental compose files (`docker-compose.dev.yml`, `.local.yml`, `.firefly.yml`, `.smdl.yml`, `.test.yml`) as well as numerous MCP-specific directories and scripts, suggesting multiple deployment modes (dev, local, integration) layered on top of the core MetaMCP stack. The Windows side is controlled via batch and PowerShell scripts (`scripts/START_AI_STACK.bat`, `scripts/STOP_AI_STACK.bat`, etc.) for orchestrating Docker, WSL/OpenClaw, LM Studio, and bridges.

## Threat model and trust boundaries

### Assets and data of interest
The system stores and processes several sensitive asset types:
- Long-term user memories via Memory MCP, including preferences and contextual data used for personalization.
- Reminders and scheduling data, potentially overlapping with medication, meetings, and other personal routines.
- Calendar events, email access, file contents in Google Workspace and OneDrive, and GitHub repository information.
- Downloaded media via yt-dlp and gallery-dl, which may include private links or tokens.

Compromise of the Memory MCP or any bridge that exposes memories or tool responses would leak a high-fidelity profile of the user’s behavior and accounts. Similarly, compromise of MetaMCP or OpenClaw could allow an attacker to issue arbitrary tool calls against Google, Microsoft, and GitHub on the user’s behalf.

### Assumed trust boundaries
The README explicitly states that all ports are bound to `127.0.0.1` and that external access goes exclusively through Cloudflare Tunnel for the Mini App. The expectation is therefore that:
- Local HTTP services are only reachable from the host machine.
- The Cloudflare Tunnel exposes only the Mini App dashboard, presumably with TOTP protection.
- Telegram provides the external control channel but is logically separated from internal HTTP services via the bots’ logic.

However, `sentinel_bridge.py` actually binds its HTTP server to `0.0.0.0`, not `127.0.0.1`, contradicting the README’s claim. This discrepancy widens the effective trust boundary from the local host to the entire network segment where the machine resides, which is critical when combined with weak or absent authentication on the bridge.

## Security analysis

### Unauthenticated bridge APIs
`sentinel_bridge.py` defines a simple HTTP server on port 8097 that serves the dashboard HTML and exposes three APIs: `/api/status`, `/api/memories`, and `/api/memories/stats`. The code directly calls helper functions (`_get_status`, `_get_memories`, `_get_memory_stats`) and emits JSON responses without any authorization checks or authentication tokens.

The `_get_memories` function issues an HTTP request to `http://127.0.0.1:8092/memories` with optional `limit` and `tag` parameters, and simply returns the parsed JSON to the caller, while `_get_memory_stats` returns global memory statistics. If the bridge is reachable from other hosts (which it is, due to the 0.0.0.0 binding), any client can enumerate and retrieve stored memories and metadata without needing Telegram session access or TOTP credentials.

### Network binding and CORS configuration
The bridge’s HTTP server is constructed with `("0.0.0.0", PORT)`, exposing it on all interfaces, and the handler’s `_cors` method sets `Access-Control-Allow-Origin: *` along with permissive methods and headers. This combination allows any web page (including untrusted sites) to call the bridge’s APIs from JavaScript and read the responses, as CORS explicitly allows cross-origin access.

Because the README states that all ports are bound to loopback only, operators may assume that simply running the stack does not expose the bridge beyond the local host. In reality, exposure depends on host firewall configuration, network topology, and whether the machine is joined to a shared or corporate LAN, increasing the risk that sensitive data leaks via browser-based attacks or direct HTTP calls.

### Insecure default credentials in Docker configuration
`docker-compose.yml` sets environmental defaults inside the `app` service for `POSTGRES_USER`, `POSTGRES_DB`, and critically `POSTGRES_PASSWORD` (default `m3t4mcp`) and `BETTER_AUTH_SECRET` (default `your-super-secret-key-change-this-in-production`). These values are used both to construct `DATABASE_URL` and to configure PostgreSQL itself via the `postgres` service environment.

If `.env` is missing or incomplete, Docker will still start the stack using these defaults, resulting in a PostgreSQL instance with a known password and an application auth secret that may be trivial to brute-force or guess. This is particularly problematic if PostgreSQL’s mapped port (`${POSTGRES_EXTERNAL_PORT:-9433}:5432`) is reachable from other machines, or if the MetaMCP application exposes any auth flows based on `BETTER_AUTH_SECRET`.

### LM Studio API key handling
`infer_bridge.py` optionally retrieves an LM Studio API key from the host’s keyring using the service name `"sentinel-watchdog"` and account `"lm_api_key"`. If keyring is unavailable or the key query fails, it silently falls back to an empty string and performs unauthenticated requests to LM Studio’s `/v1/models` and inference endpoints.

This design ensures compatibility, but it also means that if LM Studio is configured with its own auth and the key is absent or misconfigured, the bridge may misbehave in subtle ways, such as serving stale model lists or returning confusing errors without a clear indication that authentication is failing. Clearer failure modes and explicit logging around missing API keys would help operators detect configuration drift before it becomes a production issue.

## Reliability and resilience analysis

### MetaMCP service restart behavior
The `app` service in `docker-compose.yml` uses `restart: "no"`, whereas the `postgres` service uses `restart: unless-stopped` and includes a healthcheck based on `pg_isready`. This asymmetry means the database will automatically restart on failure, but the core MetaMCP application will not, even though README positions MetaMCP as central to AI assistant operation.

Without automatic restart, transient failures (crashes, out-of-memory events, host resource contention) can leave the stack partially down until manually intervened, despite the presence of a Watchdog bot whose purpose is to monitor and restart services. Aligning restart policies and potentially wiring healthchecks for the application service would improve resilience.

### Inference bridge concurrency and blocking model
`infer_bridge.py` uses a global `_active_count`, `_current_model`, `_blocked` flag, and `_active_connections` list, all protected by a single threading lock, to manage inference status and power-saving behavior. The bridge distinguishes inference requests from other traffic by checking for POST requests whose path contains `"completions"`, then increments `_active_count` and potentially blocks new requests if `_blocked` is set.

Connections are tracked in `_active_connections`, and the `/infer_block` endpoint sets `_blocked` to `True` and attempts to close all active connections to quickly shed load during gaming sessions or other high-usage periods. While effective at the scale of a personal assistant, this model can become fragile as concurrency increases, since errors during request handling, connection closure, or misordered lock acquisition could leave inconsistent counts or dangling connections.

### Model loading and fallback behavior
The inference bridge includes a small cache for loaded models, retrieved via LM Studio’s `/v1/models` API and cached for 15 seconds. When routing, the bridge first determines an intended model (simple, complex, or coding) based on prompt classification, then resolves to a model actually loaded in LM Studio, falling back to alternatives if necessary.

This design is pragmatic, yet it introduces a timing window in which the cache might be stale—for example, if the user swaps models manually in the LM Studio UI while the bridge continues to believe a model is loaded. In those cases, the requested model might be missing and LM Studio returns a non-200 status, which the bridge forwards without additional context to the caller. More explicit signaling around model load failures and cache invalidation would help diagnose issues.

## Architectural and maintainability considerations

### Compose and configuration sprawl
The repository root lists multiple compose files and configuration templates, including `docker-compose.dev.yml`, `docker-compose.local.yml`, `docker-compose.firefly.yml`, `docker-compose.smdl.yml`, `docker-compose.test.yml`, `.env.local.template`, `config.example.json`, and `watchdog`-specific configs. While this supports flexible deployment scenarios, it also creates cognitive load and increases the risk that operators will run the wrong combination of files or forget to apply essential overrides.

The README’s quick-start section instructs copying `config.example.json` to `config.json` and setting a minimal `.env.local` with `POSTGRES_PASSWORD`, `BETTER_AUTH_SECRET`, and `GITHUB_PAT`, but does not fully explain how each compose variant should be used or layered. A clearer configuration matrix would make operational behavior more predictable across machines and environments.

### Documentation drift and port inconsistencies
As noted, `sentinel_bridge.py`’s header comment refers to port 8096, while the script actually defines `PORT = 8097`. The README’s diagram and text emphasize that all ports are bound to `127.0.0.1`, yet the bridge explicitly binds to `0.0.0.0`. These inconsistencies are subtle but impactful, particularly when debugging networking issues or configuring firewalls.

Over time, as new MCP services and bridges are added, drift between diagrams, comments, and real configuration is likely to increase unless documentation is treated as a first-class part of the release process. Automated generation of port and endpoint tables from configuration files could help keep visual and textual documentation accurate.

### Heuristic routing design for models
`infer_bridge.py` uses keyword-based classification to choose between `SIMPLE_MODEL`, `COMPLEX_MODEL`, and `CODING_MODEL`. It constructs a combined conversation string from user and assistant messages, strips tool roles, and then looks for code fences or specific keyword sets (`_CODING_KEYWORDS`, `_COMPLEX_KEYWORDS`, `_TOOL_KEYWORDS`, `_SIMPLE_KEYWORDS`) to decide which model to route to.

This approach is lightweight and does not require an additional model, but it is brittle with respect to variations in phrasing, language, and emerging usage patterns. For example, a user request that implies coding without using any of the enumerated keywords might be misclassified as simple or complex, and future prompt styles might evade the current heuristics.

## Recommended improvements

### Strengthen internal service security
- Bind bridge servers to `127.0.0.1` unless there is a documented, auditable reason to expose them on `0.0.0.0`; if remote access is required, front the services with an authenticated reverse proxy.
- Replace wildcard CORS in `sentinel_bridge.py` with an allowlist of trusted origins (e.g., explicit localhost ports or the Mini App origin) and consider disabling CORS entirely if the dashboard is served from the same origin.
- Require a shared secret, session token, or short-lived signed token on all `/api/*` endpoints, and ensure that the bridge enforces that only authorized callers can read from Memory MCP and other sensitive services.

### Enforce secure configuration by default
- Remove hardcoded secret defaults (database passwords, auth secrets) from `docker-compose.yml`, and fail fast when required environment variables are missing.
- Provide a separate `.env.example` with commented guidance for generating strong secrets, rather than embedding weak defaults in compose files.
- Add a startup check or health endpoint that validates the presence and strength of secrets and clearly reports misconfiguration in logs or via the Watchdog.

### Improve resilience and observability
- Update the `app` service to use `restart: unless-stopped` or `restart: always`, consistent with the PostgreSQL service, and add an application-level healthcheck that exercises critical endpoints.
- Extend the Sentinel and inference bridges with `/health` endpoints that verify both their own readiness and connectivity to downstream services, enabling the Watchdog to perform more meaningful checks.
- Instrument the inference bridge with metrics for active requests, blocked periods, model swap counts, model load failures, and classification decisions, so that behavior can be inspected over time.

### Refactor bridge logic for robustness
- Replace global concurrency state in `infer_bridge.py` with a small, well-tested state manager object or class, limiting the surface for race conditions and providing clearer semantics for blocking and unblocking.
- Consider introducing bounded concurrency or a simple queue for inference requests, along with explicit backpressure (e.g., 429 responses) when capacity is exceeded, rather than relying on LM Studio’s internal queuing.
- Add unit tests around classification behavior and fallback routing to ensure that updates to keyword lists or model IDs do not unintentionally degrade behavior.

### Reduce configuration and documentation drift
- Introduce a simple configuration manifest that lists all services, ports, and bindings, and use it as the single source of truth to generate diagrams and port tables in the README.
- Document the intended compose layering and provide standard commands for each deployment mode (e.g., `local`, `dev`, `with-firefly`), reducing the likelihood of running incompatible combinations.
- Add a release checklist that includes verifying that code comments, README sections, and diagrams match the actual configuration in compose files and bridge scripts.

## Conclusion
Sentinel Stack delivers a powerful, highly integrated local assistant experience with rich tooling and automation, but this complexity brings security and reliability challenges that must be addressed systematically. The most urgent priorities are closing the gap between assumed loopback-only exposure and the reality of the bridge’s `0.0.0.0` binding, enforcing authentication and tighter CORS on internal APIs, and eliminating insecure configuration defaults.

Beyond these immediate fixes, strengthening restart behavior, improving observability for inference and model routing, and reducing configuration and documentation drift will make the system easier to operate and safer to evolve. Treating the stack as a small production platform—with clear trust boundaries, secure defaults, health-aware orchestration, and robust internal interfaces—will enable continued expansion of capabilities without sacrificing security or reliability.