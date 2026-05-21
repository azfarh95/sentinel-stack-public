# Sentinel Inference Bridge

Transparent HTTP proxy on port 8095 sitting between OpenClaw and LM Studio. Adds three things on top of pass-through proxying:

1. **3-way model routing** — picks the right local model per prompt (simple chat / complex tool calls / coding)
2. **Power-conflict protection** — blocks new inference and aborts in-flight streams when a Steam game is detected
3. **Availability-aware fallback** — checks LM Studio's loaded models and falls back gracefully when the picked target isn't loaded

- **Source:** `infer_bridge.py` (Windows-side, runs as `pythonw.exe` via Task Scheduler)
- **OpenClaw `baseUrl`:** `http://127.0.0.1:8095/v1` — must point here, not direct to LM Studio
- **Backing LM Studio:** `127.0.0.1:1234`
- **Status endpoint:** `GET /infer_status` (used by power monitor + mini app)

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                          REQUEST PATH (every call)                           │
└──────────────────────────────────────────────────────────────────────────────┘

  OpenClaw  ──POST /v1/chat/completions──►  bridge :8095
  (WSL2)                                     │
                                             │
                                  ┌──────────┴───────────┐
                                  ▼                      ▼
                          ┌──────────────┐      ┌─────────────────┐
                          │ blocked?     │ yes  │ 503 immediately │
                          │ (power_      ├─────►│ + abort all     │
                          │  conflict)   │      │ in-flight conns │
                          └──────┬───────┘      └─────────────────┘
                                 │ no
                                 ▼
                          ┌─────────────────┐
                          │  _classify()    │  ─► CODING_MODEL  (code blocks / dev keywords)
                          │  picks intended │  ─► COMPLEX_MODEL (tool calls / long prose)
                          │  model          │  ─► SIMPLE_MODEL  (greetings / short queries)
                          └────────┬────────┘
                                   ▼
                          ┌─────────────────┐
                          │ _resolve_target │  loaded models from LM Studio /v1/models
                          │ (15s cache,     │  with Bearer = WCM lm_api_key
                          │  Bearer auth)   │
                          └────────┬────────┘
                                   ▼
                       intended model loaded?
                                   │
                       ┌───────────┴───────────┐
                       ▼ yes                no ▼
                  use intended         walk fallback chain:
                                       CODING → COMPLEX → SIMPLE
                                       COMPLEX → CODING → SIMPLE
                                       SIMPLE → COMPLEX → CODING
                                   │
                                   ▼
                  rewrite request body's `model` field
                                   │
                                   ▼
                  forward to LM Studio :1234
                                   │
                                   ▼
                  stream response back to OpenClaw
```

---

## Three-way classifier

`_classify(messages)` returns one of three constants:

| Constant | LM Studio model id | Triggers |
|---|---|---|
| `CODING_MODEL`  | `qwen/qwen2.5-coder-32b-instruct` | Triple-backtick code fence anywhere in convo, or any `_CODING_KEYWORDS` (function, refactor, debug, regex, typescript, rust, …) in the last user message |
| `COMPLEX_MODEL` | `qwen/qwen3.6-27b`                | Any `_COMPLEX_KEYWORDS` (analyze, implement, write, plan, …) or `_TOOL_KEYWORDS` (calendar, gmail, weather, drive, …) — 9B handles tool calls poorly so tool-requiring intents force complex |
| `SIMPLE_MODEL`  | `qwen/qwen3.5-9b`                  | Short greetings / time queries / unmatched short prompts. Currently dormant in production (not loaded). |

**Precedence:** coding > complex > simple. A message with both code and tool keywords routes to coding.

**Tool-result blob detection:** if the last user message looks like a tool-result blob (>300 chars or starts with `{` / `[`), the classifier walks back to find the real query. Prevents tool-result echo-back from forcing complex routing.

---

## Power-conflict protection

The crib watchdog detects Steam gaming sessions via Home Assistant. On game start it POSTs `/infer_block` to the bridge:

- New inference requests get **503 Service Unavailable** immediately
- All in-flight `http.client.HTTPConnection` instances are closed, aborting any currently-streaming LM Studio responses

On game end, watchdog POSTs `/infer_unblock` — bridge resumes accepting requests.

**Why:** 7900 XTX (355W TBP) + AMD CPU + system pulls ~600W under inference. Adding a game peak (full GPU draw) crosses the 650W PSU limit. Hard interlock prevents brownouts.

**Manual control endpoints:**

| Method / Path | Effect |
|---|---|
| `POST /infer_block` | Block new requests, abort in-flight |
| `POST /infer_unblock` | Resume |
| `GET /infer_status` | Returns `{active, model, blocked, loaded}` |
| `GET /infer_status?force=1` | Same, but invalidates the loaded-models cache first (used by the mini app refresh button) |

---

## Availability-aware fallback

Without this layer, the bridge would happily rewrite to a model that LM Studio doesn't have loaded → 404 from upstream.

`_get_loaded_models()` polls `GET http://127.0.0.1:1234/v1/models` with `Authorization: Bearer <lm_api_key>` (key from Windows Credential Manager `sentinel-watchdog/lm_api_key`). Cached 15 seconds; `?force=1` on `/infer_status` invalidates the cache for user-initiated refreshes.

`_resolve_target(intended)` returns either the intended model (if loaded) or the first match from a preference-ordered fallback chain:

```
CODING_MODEL  → [COMPLEX_MODEL, SIMPLE_MODEL]
COMPLEX_MODEL → [CODING_MODEL,  SIMPLE_MODEL]
SIMPLE_MODEL  → [COMPLEX_MODEL, CODING_MODEL]
```

If nothing matches the chain, returns the first loaded model — last-resort guarantee that the rewrite never targets a phantom.

`/infer_status` also corrects the displayed `_current_model` if the cached value isn't loaded right now — keeps the mini app honest.

---

## Model swap behaviour

With ~24 GB VRAM (RTX 3090/4090, 7900 XTX), only **one** of {27B chat, 32B coder} fits at a time. LM Studio swaps in on first request — typical warm-disk swap is 5–10 seconds.

- First coding prompt after a chat session → ~5–10 s "thinking" delay (swap)
- Subsequent coding prompts within a few minutes → hot, normal latency
- Switching back to chat → another swap

The bridge doesn't initiate swaps directly. It just rewrites the model field; LM Studio decides whether to swap or 404. The fallback chain handles 404s by retrying with whatever's loaded.

---

## Why HTTP/1.0?

```python
class BridgeHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
```

HTTP/1.0 closes the connection after each response, which signals end-of-body to the client without needing `Content-Length` or chunked encoding. Streaming responses from LM Studio go through cleanly without us computing lengths or re-framing chunks.

Trade-off: no connection reuse. For OpenClaw's traffic pattern (one model call per agent turn) that's fine.

---

## Configuration

| Constant | Default | Purpose |
|---|---|---|
| `PORT` | 8095 | Bridge listen port |
| `LM_HOST` / `LM_PORT` | `127.0.0.1:1234` | LM Studio backend |
| `PROXY_TIMEOUT` | 600 s | Cold model load can take 60–120 s before first response byte |
| `_LOADED_TTL` | 15 s | Loaded-models cache lifetime |
| `SIMPLE_MODEL` | `qwen/qwen3.5-9b` | Light/fast model id |
| `COMPLEX_MODEL` | `qwen/qwen3.6-27b` | Default capable model |
| `CODING_MODEL` | `qwen/qwen2.5-coder-32b-instruct` | Code-focused model |

Model ids must match exactly what LM Studio reports on `/v1/models`. If LM Studio names them differently, edit the constants in `infer_bridge.py`.

---

## Deployment

Runs as a Windows process via Task Scheduler (`pythonw.exe` so console output is suppressed). Restart paths:

- **Mini app:** Settings → Inference → "Restart Inference Bridge" — kills the :8095 listener and respawns
- **Watchdog auto-restart:** if the bridge dies, watchdog detects (port-down) and respawns within ~30 s
- **Manual:** `taskkill /F /PID <pid>; Start-Process pythonw.exe -ArgumentList infer_bridge.py`

The status endpoint is what the mini app polls for "Inference Active / Idle" indicator and what the power monitor polls for spike classification.
