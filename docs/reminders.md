# Sentinel Reminders

Time-triggered Telegram messages with cron, interval, or one-shot schedules. The reminder pipeline is **completely decoupled from the LLM** — at fire time it's a single HTTPS POST to Telegram, no Qwen, no OpenClaw, no GPU.

- **Container:** `reminders-mcp`
- **MCP port:** 8087
- **Source:** `reminders-mcp/app/`
- **Persistence:** `/data/scheduler.db` (APScheduler jobs), `/data/reminders.db` (your metadata)

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              CREATION (sync)                                 │
└──────────────────────────────────────────────────────────────────────────────┘

  ┌────────────────┐                      ┌─────────────────────────┐
  │ Mini App form  │                      │ Telegram chat with you  │
  │ (Reminders →   │                      │ ↳ Claude (OpenClaw +    │
  │  Add)          │                      │   LM Studio Qwen 27B)   │
  └────────┬───────┘                      └────────────┬────────────┘
           │ POST /api/reminders                       │ tool call
           │ {message,when,target,contact_ids[]}       │ add_reminder(...)
           ▼                                           ▼
  ┌────────────────┐                      ┌─────────────────────────┐
  │ bridge.py      │                      │ MetaMCP (:12008) →      │
  │ :8098          │                      │ namespace fan-out       │
  └────────┬───────┘                      └────────────┬────────────┘
           │                                           │
           │  RemindersMCPClient.add(...)              │
           └──────────────┬────────────────────────────┘
                          │ MCP streamable-HTTP
                          ▼
              ┌─────────────────────────┐
              │ reminders-mcp :8087     │
              │ FastMCP server          │
              │                         │
              │  add_reminder tool:     │
              │   1. parse_when(when)   │  ─→ {"trigger_type":"date|cron|interval",
              │                         │      "trigger_kwargs":{...}}
              │   2. uuid4()[:8] = id   │
              │   3. scheduler.add_job  │
              │   4. db.create_reminder │
              └────────┬────────────┬───┘
                       ▼            ▼
              ┌────────────────┐   ┌──────────────────┐
              │ reminders.db   │   │ scheduler.db     │
              │ id, message,   │   │ APScheduler job  │
              │ next_run,      │   │ table (pickled,  │
              │ recipients,    │   │ persists across  │
              │ status         │   │ container restart)│
              └────────────────┘   └──────────────────┘


┌──────────────────────────────────────────────────────────────────────────────┐
│                            FIRING (async, in-process)                        │
└──────────────────────────────────────────────────────────────────────────────┘

         tick   tick   tick   tick   tick   ⚡ FIRE!
   ─────│──────│──────│──────│──────│──────────────►  wall clock
        │      │      │      │      │
        ▼      ▼      ▼      ▼      ▼
   ┌─────────────────────────────────────────────┐
   │ AsyncIOScheduler  (asyncio loop in the      │
   │ reminders-mcp container)                    │
   │                                             │
   │  • watches scheduler.db job triggers        │
   │  • misfire_grace_time = 3600s (1h late ok)  │
   │  • coalesce = true (collapse missed runs)   │
   └──────────────────┬──────────────────────────┘
                      │ next_run_time reached
                      ▼
        ┌───────────────────────────────┐
        │ fire_reminder(id, chat_id,    │
        │   message, trigger_type,      │
        │   recipients[])               │
        └──────────┬──────────┬─────────┘
                   │          │  for r in recipients:
                   ▼          ▼
        ┌──────────────────────────────────┐
        │ httpx POST                       │
        │   api.telegram.org/bot<TOK>/     │
        │   sendMessage                    │
        │ (TOK = TELEGRAM_BOT_TOKEN env;   │
        │  same as @YourSentinelBot)       │
        └────────────┬─────────────────────┘
                     ▼
            ⏰ Reminder lands in Telegram

            POST-FIRE:
              db.mark_fired(id)
              if trigger_type == "date":
                db.mark_completed(id)
              else:
                cron/interval rearms itself
```

---

## What this gets you

| Scenario | Reminder still fires? |
|---|---|
| LM Studio crashed | ✅ |
| OpenClaw restarting | ✅ |
| Gaming session active (inference bridge blocked) | ✅ |
| Internet flakey but Telegram reachable | ✅ |
| `reminders-mcp` container down | ❌ — queues; fires on restart within `misfire_grace_time = 1h` |

The firing path never touches the LLM. Blocking the inference bridge during a gaming session does **not** break reminders.

---

## Creation paths

### Mini App (no LLM)

Form at `Reminders → + Add`. Submits to `bridge.py:/api/reminders`:

```json
{
  "message": "Take medication",
  "when":    "tomorrow 9am",
  "label":   "meds",
  "target":  "dm" | "group" | "contacts",
  "contact_ids": ["5712909338", "..."]
}
```

When `target=contacts`, the first `contact_ids[0]` becomes the primary recipient and the rest go to `recipients[]`.

**Cost per creation:** ~5 ms, 0 tokens, 0 GPU.

### Telegram chat with Claude (LLM-mediated)

You write "remind me to pick up groceries at 6pm" in chat. OpenClaw routes the message through MetaMCP, the agent emits an `add_reminder` tool call.

**Cost per creation:** ~one Qwen 27B inference (~2–4 sec, ~300W GPU spike).

---

## Schedule formats

`parse_when()` accepts natural language and emits one of three APScheduler trigger types:

| Input | Trigger type | Example trigger_kwargs |
|---|---|---|
| `tomorrow 9am`, `next Friday 3pm`, `2026-05-10T09:00:00`, `in 30 minutes` | `date` | `{"run_date": "..."}` |
| `every day at 9am`, `every Monday at 8am`, `every weekday at 9am` | `cron` | `{"day_of_week": "mon", "hour": 8}` |
| `every 30 minutes`, `every hour` | `interval` | `{"minutes": 30}` |
| `0 9 * * 1` (5-field cron) | `cron` | `{"minute": 0, "hour": 9, "day_of_week": 1}` |

`date` triggers fire once and complete. `cron` and `interval` self-rearm.

---

## Multi-recipient delivery

A single reminder fans out to one primary `chat_id` plus optional `recipients[]`:

```python
fire_reminder(chat_id="...", message="...", recipients=["...", "...", "..."])
```

The fire function POSTs `sendMessage` once for the primary, then loops the recipients. Failures on individual targets are logged but don't abort the others. Duplicate IDs (recipient == primary) are skipped.

The mini app's Reminders form gets recipients from `/api/contacts`, which is sourced from OpenClaw's `~/.openclaw/credentials/telegram-pairing.json` — anyone who's done `/start` with `@YourSentinelBot` is automatically in the contact picker.

---

## Resilience

| Knob | Value | Meaning |
|---|---|---|
| `misfire_grace_time` | `3600` (1h) | If the container was down at the scheduled time, fire on restart up to 1h late |
| `coalesce` | `true` | If multiple firings were missed during an outage, collapse into one |
| Job store | SQLAlchemy → `/data/scheduler.db` | All armed jobs survive `docker restart`. APScheduler reloads them on startup. |

**Restart sequence:**

```
docker restart reminders-mcp
        │
        ▼
FastMCP _lifespan starts
  if not scheduler.running:
      scheduler.start()
        │
        ▼  reads scheduler.db
All armed jobs reload
+ any missed firings within
misfire_grace get coalesced
+ fired
```

The `if not scheduler.running` guard exists because FastMCP creates a fresh lifespan per MCP session — without the guard, every reconnect would try to spawn a duplicate scheduler.

---

## MCP tools exposed

| Tool | Purpose |
|---|---|
| `add_reminder(chat_id, message, when, label?, recipients?)` | Schedule one-shot or recurring |
| `list_reminders(chat_id?, include_completed?)` | List active (and optionally completed) reminders |
| `cancel_reminder(reminder_id)` | Remove from scheduler + mark cancelled in DB |
| `update_reminder(reminder_id, message?, when?)` | Edit message or reschedule |
| `snooze_reminder(reminder_id, duration)` | Delay next fire by `1 hour`, `30 minutes`, etc. |
| `reminder_info(reminder_id)` | Full record incl. live `next_run` from scheduler |

---

## Cost / power summary

| Operation | Tokens | GPU | Wall time |
|---|---|---|---|
| Create via mini app | 0 | 0 | ~5 ms |
| Create via chat with Claude | ~one Qwen 27B call | ~300W spike | 2–4 s |
| Fire (regardless of how created) | 0 | 0 | ~50 ms |
| 365 daily firings (one year) | 0 | 0 | ~18 s total |

Recurring reminders are effectively free to run. The only LLM cost is the optional one-time intent-parsing at creation, and you can skip even that by using the mini app form.
