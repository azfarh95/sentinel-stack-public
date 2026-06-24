# TOOLS.md

Telegram chat IDs are stored in `config.json` → `telegram_chat_ids.dm` (DM) and `telegram_chat_ids.group` (Group).

---

## /dashboard Command

When the user sends `/dashboard`, POST to `http://127.0.0.1:8097/api/send-dashboard` with `{"chat_id": "<current chat_id>"}` using fetch-mcp. Reply with a single short confirmation once sent. No other output.

---

## Auto Video Download

If a message contains ONLY a URL from a supported domain (optionally followed by a quality), download it immediately — no command prefix, MCP tools only.

**Domains:** youtube.com/shorts/, youtu.be/, instagram.com, tiktok.com, twitter.com / x.com, facebook.com, reddit.com (v.redd.it).

**Workflow:**
1. `VideoDownloader__download_video(url, quality="1080p")` → `job_id`
2. Poll `VideoDownloader__check_download(job_id)` every 10–15s, send brief progress updates, until `status` is `done` or `error`.
3. `VideoDownloader__send_to_telegram(chat_id, filepath, caption=<title>)`.
4. Confirm title + size.

**Quality:** append `720p` / `480p` / `360p` / `audio-only`. Default `1080p`.

**Cookies** (auto-detected by domain): `G:\YT-DLP\cookies\<tiktok|instagram|twitter|facebook>.txt`. Manual: `cookies_file="tiktok"`.

**Limit:** 50 MB Telegram bot API. On `file_too_large`, tell the user the file is at `G:\YT-DLP\`.

---

## Auto Translate (Foreign → English)

If a message is plain non-English prose (≥3 words, not a URL/command/code/short ack), auto-translate to English without asking.

**Workflow:**
1. `detect_language(text)`. If top is `en` with `confidence ≥ 60` → STOP. If non-English with `confidence ≥ 50` → continue. Low confidence → reply normally and offer translation.
2. `translate_text(text, target="en", source="auto")`.
3. Reply:

   ```
   🌐 [<language>] → English
   <translation>
   ```

   Then continue normally (answer the question if there was one).

**Skip:** explicit "translate X" requests (use Translation rules below), URLs, code/JSON/logs, English-dominant mixed text, short acks.

---

## Translation (Explicit Asks)

For "translate X to Y" / "what does this mean" / "what language is this":

- `translate_text(text, target, source="auto")` — main call. Use `source="auto"` unless user states it.
- `detect_language(text)` — language detection only.
- `list_languages()` — list available codes.

**Locally preloaded:** `en`, `zh-Hans` (Simplified Chinese), `ru`. Other codes only work when public fallback is reachable.

**Don't use for:** transliteration (pinyin/romaji), grammar checks, paraphrasing.

---

## Maps & Directions

**Never** use Tavily for maps, directions, or location queries.

- `maps_directions(chat_id, origin, destination, mode)` — mode = `transit` (default) | `driving` | `walking` | `cycling`. Origin/destination accept postal codes, addresses, or place names.
- `maps_search(chat_id, query)` — find a place / "where is X".

Sends a Telegram button. Confirm briefly; don't look up the location yourself.

---

## Memory

**memory-mcp** — long-term recall across sessions. Use proactively.

- `memory_store` — durable facts only: user preferences/habits, completed research findings, incidents/fixes, "remember this". Tag with `["preference"]` / `["incident"]` / `["research"]` / `["config"]`.
- `memory_search` — when a question feels like history ("didn't we fix this?", "what do I prefer for X?"), or before starting a task with prior context.

Skip ephemeral chat.

---

## HTTP Fetch

**fetch-mcp** — live data from external APIs/URLs when no dedicated tool exists. Crypto prices, exchange rates, weather, REST calls, raw JSON. Pass headers/body for auth or POST.

Not for: web search (Tavily), maps (maps-mcp), video (VideoDownloader).

---

## SQLite

**sqlite-mcp** — read/write `sentinel.db` at `/workspace/sentinel.db` (= `C:\Users\azfar\sentinel.db`).

For logging events, tracking jobs, watchlists, history queries with filters/aggregations. Reach for it before falling back to a file when the user says "log", "track", "record", or "query".

---

## Git (Local) vs GitHub (Remote)

- **git-mcp** — local repos under `/workspace` (`C:\Users\azfar`). Branch state, recent commits, diffs, file history, pre-commit checks. Specify subdirectory for nested repos.
- **GitHub MCP** — anything on github.com: repos, issues, PRs, code search, Actions, releases. Full read/write via PAT, all toolsets enabled.

---

## Summarize (URLs / Files / YouTube)

When the user asks to summarize, transcribe, or "what's this about" for a URL, YouTube link, podcast, article, PDF, or local file — use the **summarize** skill (`summarize <url-or-path>`). It handles fetching, transcription, and summarization in one call.

Prefer over: fetch-mcp + LLM-summarize (extra steps), Tavily (search, not summarize).

For YouTube transcript only (no summary): `summarize <url> --youtube auto`.

---

## Ask Gemini (Second Opinion)

Use the **gemini** skill (`gemini -p "<prompt>"`) when the user wants:
- A second-model opinion on a decision, design, or answer
- To bounce a tricky question off Gemini specifically ("ask Gemini…")
- A quick one-shot generation without committing to a full agent session

Free tier: ~250 req/day on Flash. Skip for normal chat — your default model handles those.

---

## OneDrive

**OneDrive MCP** — files/documents in personal OneDrive. Excel, Word, PDF, CSV. Ideal for accounting work (P&L, invoices, expenses, audits).

1. `onedrive_search` or `onedrive_list` → get `item_id`
2. `onedrive_read(item_id)` → content
3. Analyse in your reply

If `authenticated: false`, tell the user to open `http://localhost:8093/auth` once.

Not for SharePoint or work/school accounts.
