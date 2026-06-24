<img src="https://r2cdn.perplexity.ai/pplx-full-logo-primary-dark%402x.png" style="height:64px;margin-right:32px"/>

# Sentinel Stack Weakpoints Review

## Overview

This document reviews key weaknesses identified in the `azfarh95/sentinel-stack-public` repository based on the repository README, the main Docker Compose file, and two Python bridge services: `sentinel_bridge.py` and `infer_bridge.py`.

The stack is ambitious and well-structured for a personal local-first AI assistant, but several issues increase risk in four areas: security exposure, service resilience, architectural clarity, and maintainability.

## Critical security issues

### Insecure defaults in Docker Compose

The main `docker-compose.yml` includes fallback values for `POSTGRES_PASSWORD` and `BETTER_AUTH_SECRET`, specifically `m3t4mcp` and `your-super-secret-key-change-this-in-production`. This creates a dangerous failure mode where a missing `.env` or incomplete deployment can still boot using predictable credentials.

Recommended actions:

- Remove all secret defaults and require explicit environment values at startup.
- Fail fast if `POSTGRES_PASSWORD` or `BETTER_AUTH_SECRET` is missing.
- Add a startup validation script so insecure defaults cannot be used accidentally.


### Unauthenticated memory exposure

The `sentinel_bridge.py` service exposes `/api/memories` and `/api/memories/stats`, and the handler returns data directly from the local memory service without authentication checks. Because the bridge is described as serving dashboard access and memory data, this endpoint can expose highly sensitive personal information if reachable.

Recommended actions:

- Require a session token or signed internal header on every `/api/*` route.
- Enforce authorization at the bridge layer, not only in the frontend.
- Add audit logging for all memory reads.


### Unsafe network binding and permissive CORS

The README says ports are bound to `127.0.0.1` only, but `sentinel_bridge.py` actually starts a `ThreadingHTTPServer` on `0.0.0.0`, not loopback. The same file also sends `Access-Control-Allow-Origin: *`, which means any origin is allowed to call the API.

This mismatch matters because the combination of `0.0.0.0` binding, unauthenticated APIs, and wildcard CORS can expose internal status and memory data beyond the intended local-only trust boundary.

Recommended actions:

- Bind the bridge to `127.0.0.1` unless there is a strictly necessary reverse-proxy use case.
- Replace wildcard CORS with an allowlist of exact trusted origins.
- Align the README with the actual network behavior so operators do not assume protection that is not present.


## Reliability issues

### No automatic restart for MetaMCP app

In `docker-compose.yml`, the central `app` service uses `restart: "no"`, while PostgreSQL uses `restart: unless-stopped`. Because the README describes MetaMCP as the aggregation gateway for the whole tool stack, failure of this service can take down core assistant functionality with no automatic recovery.

Recommended actions:

- Change the `app` restart policy to `unless-stopped` or `always`, depending on operational intent.
- Add healthchecks so orchestration can distinguish startup delay from failure.
- Make Watchdog verify service functionality, not only port availability.


### Fragile inference concurrency model

`infer_bridge.py` tracks request activity using shared globals such as `_active_count`, `_active_connections`, and `_blocked`, all coordinated with a single lock. While this is workable for low traffic, it becomes fragile as concurrency rises, especially when connection teardown, forced blocking, and exception handling happen at the same time.

Specific concerns:

- Connection bookkeeping relies on shared mutable state.
- Error paths can leave status reporting temporarily inaccurate.
- The bridge has no explicit request queue or backpressure control before forwarding requests to LM Studio.

Recommended actions:

- Replace ad hoc globals with a small request-state manager object.
- Introduce bounded concurrency or a queue with rejection behavior under load.
- Add metrics for active requests, rejects, forced aborts, and routing fallbacks.


### Weak operational signaling around model swaps

The bridge documentation explains that only one large model may be loaded at a time and that swaps can take several seconds to over a minute. Although the proxy timeout is extended to 600 seconds, there is no explicit user-facing signal that a delay is due to model loading rather than an application hang.

Recommended actions:

- Return intermediate status or structured progress when a model swap is expected.
- Surface “loading model” vs. “generating response” in status telemetry.
- Record model load latency separately from inference latency.


## Architecture and maintainability issues

### Documentation drift

The top comment in `sentinel_bridge.py` says `Port 8096`, but the constant in the file sets `PORT = 8097`. The README also states that all ports are loopback-only, which conflicts with the actual `0.0.0.0` binding in the bridge.

These inconsistencies reduce operator trust in the docs and make troubleshooting slower, especially for a system with many moving parts and ports.

Recommended actions:

- Add a documentation consistency pass to the release checklist.
- Generate port maps from source configuration where possible.
- Treat comments and README networking claims as configuration-controlled documentation.


### Compose sprawl without clear layering

The repository root contains multiple compose files, including `docker-compose.yml`, `docker-compose.dev.yml`, `docker-compose.local.yml`, `docker-compose.firefly.yml`, `docker-compose.smdl.yml`, and `docker-compose.test.yml`. Without a clearly documented override strategy, operators may not know which files are baseline, additive, or mutually exclusive.

Recommended actions:

- Define one canonical base file and document all overlays explicitly.
- Provide named startup commands for each supported mode.
- Add a matrix showing which services are enabled by each compose profile.


### Heuristic routing is brittle

`infer_bridge.py` uses hardcoded keyword sets for simple, complex, tool, and coding intent classification. This method is lightweight, but it is brittle against paraphrasing, multilingual input, typos, ambiguous wording, and future prompt styles.

Recommended actions:

- Move routing rules into a configuration file for faster tuning.
- Add test fixtures for representative prompt categories and misclassification cases.
- Consider confidence scoring or a smaller classifier model rather than fixed substring checks alone.


## Priority remediation plan

| Priority | Issue | Why it matters | Suggested fix |
| :-- | :-- | :-- | :-- |
| P0 | Unauthenticated memory APIs. | Exposes personal memory data directly. | Add auth on every API route, not just frontend gating. |
| P0 | `0.0.0.0` binding plus wildcard CORS. | Expands exposure beyond intended local-only trust boundary. | Bind to loopback and restrict origins. |
| P0 | Hardcoded fallback secrets. | Enables insecure startup with predictable credentials. | Remove defaults and fail startup if unset. |
| P1 | `restart: "no"` on MetaMCP. | Central gateway may not recover automatically after failure. | Enable restart policy and add healthchecks. |
| P1 | Fragile inference request tracking. | Makes correctness and observability weaker under concurrency. | Refactor to structured state management and bounded concurrency. |
| P2 | Documentation drift and compose sprawl. | Slows debugging and increases operator mistakes. | Standardize docs, ports, and compose layering. |

## Final assessment

The strongest immediate concern is not code style or performance, but trust boundary failure: the bridge layer currently appears to expose sensitive capabilities and data with insufficient enforcement. After that, the next most important weakness is operational resilience, especially the lack of auto-restart for the core MetaMCP service and the fragile concurrency assumptions inside the inference bridge.

The overall system design is powerful, but it is now complex enough that it should be treated like a small production platform rather than a collection of local scripts. That means stronger defaults, explicit trust boundaries, health-aware orchestration, and tighter configuration discipline are the changes most likely to improve safety and long-term maintainability.

