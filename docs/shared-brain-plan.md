# Sentinel Shared Brain — implementation plan

**Status:** **SHIPPED** — verified against code 2026-06-03. Phases 0–5 + 7 live since 2026-05-30: Telegram + web Mini App both run on `brain_store` (Postgres) with the `/api/brain/*` REST suite, `/ws/brain` WebSocket push, forum-topic threading, and rolling summarization. **Still partial:** Phase 6 (Tauri chat panel — not in this repo) and Phase 8 (`/forget` exists as a `brain_store` method but isn't wired as a route; rate-limiting is auth-only; no token-usage reporting). Comet runs stateless (not brain-wired). Ratified by [ADR AI-001](https://docs.your-domain.example.com/adrs/ai/001-shared-brain-owns-transport/). *(Phase notes below were the original 2026-05-26 plan.)*
**Owner:** Azfar (single-user V4 Mode A — owner-only).
**Cross-pillar:** AI (OpenClaw + MetaMCP), Watchdog (admin app reuses WS pattern), Infra (Mini App + TWA).

---

## Goal

Give OpenClaw a **single persisted conversation state** that every Sentinel
client surface reads from and writes to, so the assistant carries the same
context regardless of where you message it.

Today: each surface (Telegram bot, Mini App, TWA, Tauri admin) opens its
own ephemeral session against OpenClaw. State dies when the process
restarts. Cross-device handoffs are impossible.

Target: 4 surfaces share one named-threaded conversation store. You start
on Telegram, walk to laptop, open Mini App, see latest exchange already
there, continue. WebSocket push delivers new messages real-time to all
open surfaces.

---

## Non-goals (out of scope for v1)

- **Multi-user.** You're owner-only per V4 Mode A. Adding `user_id` to
  the schema costs nothing; real multi-tenant isolation is V7 work.
- **Infinite context.** The backend has a fixed context window
  (`llama-server` runs Qwen 3.6 27B at 131k). Summarization
  helps but doesn't make the brain unbounded.
- **Cross-conversation RAG.** "What did we discuss about Maybank 3 weeks
  ago?" — that's retrieval over the message archive, separate project.
- **Notification routing.** "You have 3 unread messages on Telegram"
  type cross-surface awareness. Different problem.

---

## Architecture

```
                    SENTINEL SHARED BRAIN — TARGET STATE
                    ═══════════════════════════════════

   ┌────────────┐   ┌────────────┐   ┌────────────┐   ┌────────────┐
   │ Telegram   │   │ Mini App   │   │ TWA        │   │ Tauri      │
   │ bot        │   │ (browser)  │   │ (Android)  │   │ admin      │
   └─────┬──────┘   └─────┬──────┘   └─────┬──────┘   └─────┬──────┘
         │                │                │                │
         │ send/receive   │ WS + REST      │ WS + REST      │ WS + REST
         │ via Bot API    │                │ (over webview) │
         │                │                │                │
         └────────┬───────┴────────┬───────┴────────┬───────┘
                  │                │                │
                  ▼                ▼                ▼
            ┌─────────────────────────────────────────────┐
            │   /api/brain/*  (REST)                      │
            │   /ws/brain     (WebSocket push)            │
            │   on metamcp-local API service              │
            └───────────────────┬─────────────────────────┘
                                │
                  ┌─────────────┼─────────────┐
                  │             │             │
                  ▼             ▼             ▼
         ┌────────────┐  ┌────────────┐  ┌────────────┐
         │ brain_     │  │ OpenClaw   │  │ MetaMCP    │
         │ store.py   │◄─┤ orchestr.  │──┤ gateway    │
         │            │  │            │  │ (12+ MCPs) │
         └──────┬─────┘  └──────┬─────┘  └────────────┘
                │               │
                ▼               ▼
         ┌────────────┐  ┌────────────┐
         │ Postgres   │  │llama-server│
         │ metamcp-pg │  │ :1234      │
         └────────────┘  └────────────┘
```

**Why this shape:**
- `brain_store` is a thin module, not a new service — runs in-process with the API.
- Reuses `metamcp-postgres` (already up) — no new container.
- Reuses Mini App's existing FastAPI + WS infrastructure (the watchdog v2 pattern at `/ws/v2/health` is the template).
- OpenClaw stays mostly intact; gets a thin wrapper that reads context from brain_store on entry and appends to it on exit.

---

## Data model

```sql
-- One row per logical conversation thread
CREATE TABLE conversations (
    id              UUID PRIMARY KEY,
    user_id         TEXT NOT NULL,        -- 'azfar' for now; multi-user later
    name            TEXT NOT NULL,        -- 'default', 'Finance work', etc.
    kind            TEXT DEFAULT 'general', -- 'general' | 'finance' | 'gaming' | etc.
    pinned_context  TEXT,                 -- system-prompt overlay specific to this thread
    started_at      TIMESTAMPTZ NOT NULL,
    last_active_at  TIMESTAMPTZ NOT NULL,
    archived_at     TIMESTAMPTZ,
    UNIQUE (user_id, name)
);
CREATE INDEX idx_conversations_user_active
    ON conversations(user_id, last_active_at DESC)
    WHERE archived_at IS NULL;

-- One row per message in any conversation
CREATE TABLE messages (
    id             BIGSERIAL PRIMARY KEY,
    conv_id        UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role           TEXT NOT NULL,         -- 'user' | 'assistant' | 'tool' | 'system'
    content        TEXT NOT NULL,
    surface        TEXT,                  -- 'telegram' | 'miniapp' | 'twa' | 'tauri' | 'cli'
    surface_msg_id TEXT,                  -- e.g. Telegram message_id, for de-dup + reply-threading
    tool_calls     JSONB,                 -- OpenAI/MCP tool-call objects when role='assistant'
    tool_result    JSONB,                 -- when role='tool', the call's output
    parent_msg_id  BIGINT REFERENCES messages(id),  -- for tool-result threading
    tokens_in      INTEGER,               -- prompt tokens this turn (assistant rows)
    tokens_out     INTEGER,               -- completion tokens
    model          TEXT,                  -- which LM Studio model handled it
    is_summary     BOOLEAN DEFAULT FALSE, -- TRUE for rolling-summary placeholder rows
    pinned         BOOLEAN DEFAULT FALSE, -- never prune even when window is full
    created_at     TIMESTAMPTZ NOT NULL,
    streaming_done BOOLEAN DEFAULT TRUE   -- FALSE during in-progress assistant generation
);
CREATE INDEX idx_messages_conv_time ON messages(conv_id, created_at);
```

**Schema decisions:**
- **Threads first-class** (named per user). Default thread auto-created on first message.
- **`surface` tagged on every message** — lets clients show "via Telegram" badges + filter if needed.
- **`is_summary`** rows hold LLM-generated summaries of older message ranges (see Context Window section).
- **`pinned`** lets you mark a message as never-prune (system prompts, key references).
- **`streaming_done`** supports partial-token streaming via WebSocket without polluting the final transcript.

---

## API surface

### REST — `/api/brain/*` on the metamcp-local API service

```
GET    /api/brain/threads                    → list user's threads (active + archived)
POST   /api/brain/threads                    → create a new thread (body: {name, kind?})
GET    /api/brain/threads/{id}/messages?since=… → load message history
POST   /api/brain/threads/{id}/messages      → POST a user message (returns assistant response;
                                               WS subscribers see the streaming version)
POST   /api/brain/threads/{id}/archive       → soft-archive
POST   /api/brain/threads/{id}/forget        → "forget last N messages" (sets is_summary on a
                                               replacement summary row; doesn't hard-delete)
POST   /api/brain/threads/{id}/pin/{msg_id}  → pin a message
```

### WebSocket — `/ws/brain`

Mirror of the watchdog v2 pattern. Client subscribes; server pushes events.

```json
{ "kind": "message.new",      "thread_id": "…", "message": {…} }
{ "kind": "message.partial",  "thread_id": "…", "message_id": 12345, "delta": "…token…" }
{ "kind": "message.complete", "thread_id": "…", "message_id": 12345 }
{ "kind": "thread.updated",   "thread_id": "…", "field": "name|archived_at|pinned_context" }
```

Subscription model: client sends `{ "subscribe": ["thread:<id>", "user:<id>"] }` on connect.
Server filters events accordingly.

---

## Per-surface adapter design

### Telegram bot

Currently OpenClaw runs sessions keyed by Telegram chat_id with in-memory state.

**Changes:**
- Add a `thread_for_chat(chat_id)` lookup: defaults to user's `default` thread.
- `/threads` command: list threads.
- `/switch <name>` command: change the active thread for this chat.
- `/new <name>` command: create + switch.
- Every received message: `append(conv_id, role='user', content=…, surface='telegram', surface_msg_id=…)` then call OpenClaw with last-N from brain_store.
- Every assistant response: `append(conv_id, role='assistant', …)`.
- **No WS needed** — Telegram is naturally one-message-at-a-time.

### Mini App (browser)

Lives at `suite.your-domain.example.com` already. Gains a chat panel.

**Changes:**
- New SPA route `/chat` with thread picker sidebar + message stream + composer.
- On open: WS subscribe to `thread:<active>` → renders incoming messages live.
- On send: POST to `/api/brain/threads/{id}/messages`; UI shows optimistic user message; WS delivers streamed assistant tokens.
- Thread picker: dropdown + "New thread" button.
- Authenticated via Telegram initData (same as Mini App already does).

### TWA (Android)

`sentinel-suite-twa` wraps `suite.your-domain.example.com`. **Zero changes.** Inherits the Mini App's `/chat` route automatically.

Mini App must respect TWA quirks:
- WebView cookie isolation (per memory `feedback_telegram_miniapp_patterns`)
- CF Access strips `#tgWebAppData=...` (per memory `feedback_cf_access_strips_tg_initdata`) — already handled via the `/miniapp/session` server-side endpoint
- These patterns already work; chat just inherits them.

### Tauri admin (desktop)

Sentinel Admin .exe / .msi. Currently watchdog-focused.

**Changes:**
- Add a new Svelte route `/chat` (parallel to existing Admin / Audit / Gaming routes).
- WS subscribe via Tauri's `tauri::api::http::ws` OR plain JS WS — Tauri 2's WS support is fine for both.
- Same REST endpoints as Mini App.
- Slightly different auth: Tauri carries a device token + auto-injects it.

---

## OpenClaw integration sketch

```python
# metamcp-local/openclaw/brain_wrapper.py
async def chat_turn(thread_id: str, user_msg: str, surface: str) -> AsyncIterator[str]:
    """Append user msg, load context, call LLM, stream + persist response."""
    store = BrainStore()
    
    # 1. Persist user message immediately so all subscribers see it
    user_row = await store.append(
        conv_id=thread_id, role="user", content=user_msg, surface=surface,
    )
    await broadcast_ws({"kind": "message.new", "thread_id": thread_id,
                         "message": user_row.to_dict()})
    
    # 2. Load conversation context (token-budget aware)
    history = await store.load_for_llm(conv_id=thread_id, max_tokens=8000)
    
    # 3. Begin assistant row in streaming mode
    asst_row = await store.append(
        conv_id=thread_id, role="assistant", content="", surface="server",
        streaming_done=False,
    )
    
    # 4. Stream tokens from OpenClaw + LM Studio
    accumulated = []
    async for delta in openclaw.run(history + [{"role":"user","content":user_msg}]):
        accumulated.append(delta)
        await broadcast_ws({"kind": "message.partial",
                            "thread_id": thread_id,
                            "message_id": asst_row.id,
                            "delta": delta})
        yield delta
    
    # 5. Finalize
    final_content = "".join(accumulated)
    await store.finalize(asst_row.id, content=final_content,
                          tokens_in=…, tokens_out=…, model=…)
    await broadcast_ws({"kind": "message.complete",
                        "thread_id": thread_id, "message_id": asst_row.id})
```

**Tool calls:** when OpenClaw invokes an MCP tool mid-turn, persist a `role='tool'` row with `tool_calls` (the call) and `tool_result` (the response), `parent_msg_id` linking back to the assistant row.

---

## Context window management

Single biggest risk: blowing past LM Studio's context limit as conversations grow.

**Strategy: rolling summarization with token budget.**

1. **Token budget per turn:** parameterize per model. Default 8K-tokens for context, leaving 4K for response.
2. **Loader logic** (`brain_store.load_for_llm`):
   - Always include: `pinned=TRUE` messages (system prompts, key facts).
   - Walk backward from latest: include messages until token count exceeds budget.
   - If we hit messages older than the budget: replace them with a single `is_summary=TRUE` row covering the dropped range.
3. **Summarization:** when load_for_llm sees a gap that would drop ≥10 messages, async-trigger an LLM call to summarize that range. Store as a new `is_summary=TRUE` row in the conversation. Next load uses it.
4. **Per-thread system prompt** (`conversations.pinned_context`): allow per-thread overlay (e.g., "Finance work" thread has Sentinel Finance architecture pinned).

**Per memory `feedback_openclaw_stalled_model_call`:** body_bytes >100KB → OpenClaw aborts before first token. Token-budget loading is the structural fix.

---

## Phased rollout

Each phase has a clear verification gate. Don't move on until the previous phase verifies green.

### Phase 0 — Discovery (1-2 hours)
- Map current OpenClaw conversation flow: where state lives today, how each surface calls in.
- Confirm metamcp-postgres credentials + schema migration path.
- Lock the conversation thread default name convention.

**Verify:** discovery doc lists every call site of OpenClaw + where session state currently lives.

### Phase 1 — `brain_store` schema + module (1 day)
- Create `conversations` + `messages` tables in metamcp-postgres.
- Build `metamcp-local/openclaw/brain_store.py` with: `create_thread`, `get_thread`, `list_threads`, `append`, `finalize`, `load_for_llm`, `archive`, `pin`, `forget`.
- Unit tests: round-trip messages, list ordering, token-budget loading.
- Migration safe-additive — no changes to existing tables.

**Verify:** unit tests green; can `INSERT/SELECT` round-trip; `load_for_llm` respects token budget on a 1000-msg synthetic conversation.

### Phase 2 — OpenClaw integration (1-2 days)
- Add `brain_wrapper.py` that wraps `openclaw.run()` per the sketch above.
- Update OpenClaw entrypoint to accept `thread_id` + `surface` instead of stateful session.
- Token counting: bind to whichever tokenizer your LM Studio model uses (tiktoken proxy or model-native).

**Verify:** can drive a 3-turn conversation via Python REPL hitting `brain_wrapper.chat_turn`, see 6 rows (3 user + 3 assistant) in DB.

### Phase 3 — Telegram adapter (1 day)
- Replace in-memory session state with `brain_store` reads/writes.
- Add `/threads`, `/switch <name>`, `/new <name>` commands.
- Default thread auto-created on first DM.

**Verify:** chat with bot via Telegram, restart container, chat again — bot remembers what was said. `/threads` shows the conversation.

### Phase 4 — Mini App `/api/brain/chat` endpoint (1-2 days)
- New FastAPI router under metamcp-local API service.
- REST endpoints per the spec above.
- Chat panel UI in the existing Mini App (Svelte/Vue/vanilla — match existing stack).
- Authenticated via Telegram initData or CF Access cookie.

**Verify:** open Mini App `/chat`, see same thread Telegram was using, send message, response lands.

### Phase 5 — WebSocket push (2 days)
- Add `/ws/brain` endpoint on the metamcp-local API. Mirror watchdog v2's `/ws/v2/health` pattern.
- Broadcast: `message.new`, `message.partial`, `message.complete`, `thread.updated`.
- Mini App subscribes on open; renders streaming tokens live.
- Tauri admin subscribes from desktop.

**Verify:** open Mini App + Tauri side-by-side, type into Mini App, watch streaming appear in Tauri in real-time. Latency < 200ms locally.

### Phase 6 — Tauri admin chat panel (1 day)
- New Svelte route in `admin/src/`.
- Reuses Mini App's chat UI as a shared component if structurally sensible; otherwise a Tauri-native port.
- Same REST + WS.

**Verify:** can carry on the same conversation between Tauri and Telegram side-by-side.

### Phase 7 — Context-window mgmt + summarization (1 day)
- Implement rolling-summarization triggered when load_for_llm would drop ≥10 messages.
- Async LLM call to LM Studio with a "summarize this range" prompt.
- Store result as `is_summary=TRUE` row.
- Budget-aware loading guard.

**Verify:** synthetic 500-msg thread loads with budget=8K and includes ≥1 summary row; conversation stays coherent (manual test).

### Phase 8 — Safety + observability (1 day)
- Token usage tracking per surface (Grafana-shaped or just a /admin page).
- Rate limiting (LM Studio is single-process; needs queueing).
- `/forget` command + UI.
- Per-thread pinning of important context.

**Verify:** abuse test (10 rapid sends) queues correctly; `/forget last 5` removes them from future load_for_llm.

---

## Open risks + mitigations

| Risk | Mitigation |
|---|---|
| LM Studio API key still blocked (per memory `project_sentinel_finance_agent`) | Unblock first — this whole project depends on reachable LLM. Phase 0 includes a key check. |
| OpenClaw refactor breaks current bot | Run new path side-by-side under `OPENCLAW_BRAIN_ENABLED=1` env flag for one cycle. Old path stays available until verified. |
| Postgres becomes a bottleneck | `messages` table has straightforward write pattern; indexed (conv_id, created_at) covers the hot read. Add connection pool sizing if needed. |
| Surface drift: Mini App and Tauri diverge | Build chat UI as a shared Svelte component published from one repo, imported by both. (Already pattern for the Watchdog admin/Mini App split.) |
| Summarization quality drops conversation coherence | Pin recent N messages always raw; only summarize "old context" not "recent". User can mark anchors with pin. |
| User accidentally sends sensitive message → wants it gone | `/forget` command soft-deletes (replaces with `[redacted]` placeholder) — preserves audit trail but removes from LLM context. |

---

## Effort estimate

| Phase | Time | Critical path? |
|---|---|---|
| 0 — Discovery | 1-2 h | Yes |
| 1 — brain_store | 1 d | Yes |
| 2 — OpenClaw integration | 1-2 d | Yes |
| 3 — Telegram adapter | 1 d | Yes (proves the loop end-to-end) |
| 4 — Mini App endpoint + chat UI | 1-2 d | Yes |
| 5 — WebSocket push | 2 d | Yes (named threads + WS were both your locked decisions) |
| 6 — Tauri admin panel | 1 d | No (deferrable; cheap wrapper) |
| 7 — Context window mgmt | 1 d | Yes (without this, threads stop working as they grow) |
| 8 — Safety + observability | 1 d | No (deferrable) |

**Critical-path total: ~7-9 working days.**
**With deferrable items: ~10-12 working days.**

---

## Next concrete steps

1. **Unblock LM Studio API key** (carryover from memory `project_sentinel_finance_agent` and D5 phase). Until this is reachable from inside the `metamcp-local` container, the whole project can't run.
2. **Phase 0 discovery session** — walk through OpenClaw's current entry point. Document where state currently lives. Output: this plan gets a "Phase 0 findings" appendix.
3. **Get user sign-off on the schema** (this doc, §"Data model"). Schema migrations are the most expensive thing to change later.
4. **Then Phase 1**: ship `brain_store.py` + the two tables + unit tests.

---

## Related memory + docs

- `feedback_openclaw_drain_timeout` — OpenClaw self-heals on stuck drain via systemd timeout
- `feedback_openclaw_stalled_model_call` — model_call stall = oversized prompt; we MUST manage context
- `project_sentinel_finance_agent` — earlier "Agent Phase A" work was blocked on LM Studio key; same blocker here
- `project_watchdog_v2_complete` — watchdog `/ws/v2/health` pattern is the template for our `/ws/brain`
- `feedback_telegram_miniapp_patterns` — WebView cookie isolation, openTelegramLink, etc.
- `feedback_cf_access_strips_tg_initdata` — server-side `/miniapp/session` fallback we'll inherit
- `feedback_miniapp_is_peer_not_legacy` — Mini App is a first-class client, not a fallback
- `feedback_sentinel_natives_bat_only` — OpenClaw is native, .bat-launched; no Task Scheduler entry

---

*Drafted by Claude in session 2026-05-26. User locked: named threads + WS push + dual docs (this + sentinel-docs summary).*
