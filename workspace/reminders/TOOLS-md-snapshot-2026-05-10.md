# TOOLS.md

Telegram chat IDs — Azfar DM: `YOUR_TELEGRAM_CHAT_ID` · Group: `-1003748374568`

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

## Browser (Playwright MCP / metamcp_Playwright_Browser_*)

The CDP-attached Chromium is for **lightly-defended sites only**. Modern bot-detection (Cloudflare Turnstile, reCAPTCHA risk-scoring) will block it — that's a known limitation, not a fixable bug. Don't keep retrying.

**Detect-and-fallback signals.** If after `Browser_Navigate` + `Browser_Snapshot` the page shows any of:
- Title or visible text contains: `security verification`, `Performing security check`, `Just a moment`, `Page Unavailable`, `Access denied`, `Cloudflare`, `verify you are human`
- URL ends up at `challenges.cloudflare.com` / `*.cloudflare.com/cdn-cgi/`
- Page is essentially empty / shows only a CAPTCHA widget

→ stop browsing. Tell the user the site blocks automated browsers, then call `metamcp_BraveSearch_brave_web_search` with the original intent and answer from snippets. Don't burn more browser cycles.

**Known-hostile domains** (skip browser, go straight to brave_web_search): `investing.com`, `shopee.sg`, `lazada.sg`, `bloomberg.com`, `wsj.com`, `nytimes.com`, `linkedin.com`, `tradingview.com`, `instagram.com` (logged-out), `facebook.com` (logged-out).

**Known-friendly domains** (browser works): `wikipedia.org`, `bbc.com`, `news.ycombinator.com`, `reddit.com` (read-only), `github.com`, `stackoverflow.com`, most blogs, most government sites.

For sites where the user has an account and wants logged-in interaction, see the cookie-import flow (mini-app Browser panel → "Import Chrome cookies"). Not the agent's job to invoke that.

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

**OneDrive MCP** — files/documents in personal OneDrive. Excel, PDF, Word, CSV. Ideal for accounting work (P&L, invoices, expenses, audits).

1. `onedrive_search` or `onedrive_list` → get `item_id`
2. `onedrive_read(item_id)` → content
3. Analyse in your reply

If `authenticated: false`, tell the user to open `http://localhost:8093/auth` once.

Not for SharePoint or work/school accounts.

---

## Research Workflow (multi-source synthesis tasks)

When the user gives an open-ended research mission ("what would I want from X", "research best practices for Y"):

**Source diversity**: don't only fetch from generalist business/UX blogs. Also search community-driven sources where practitioners discuss real workflows:
- **Reddit**: `r/selfhosted`, `r/LocalLLaMA`, `r/HomeAssistant` — what people are actually building
- **Hacker News**: `news.ycombinator.com` — especially Show HN posts and threaded discussions
- **Project roadmaps** for relevant open-source tools (e.g., when researching memory: mem0, Letta, Open WebUI, AnythingLLM, Home Assistant Voice PE)
- **Specific pain-point queries**: "X complaints reddit", "Y disabled feature" — surfaces real frustrations
- **Willingness-to-pay signals**: existing pricing for Mem.ai, Rewind.ai, ChatGPT Plus tier

**Length calibration**: ask for a target page count (3-10 pages typical) at the start, or estimate based on scope. Aim for that ±20%. Note actual length in your reply summary so the user can calibrate future asks.

**Output structure** for synthesis docs: lead with executive summary + ranked top-N findings; include an honest "unrealistic for self-hosted" reality-check section; cite sources at the end with URL + topic.

---

## Self-Correction Discipline

When you announce a file write or substantive action ("Now I have substantial material, let me write the doc", "Let me save the analysis to..."), you must on the very next turn verify the action actually happened:

- **For file writes**: confirm the file exists at the announced path with non-zero content. If not, write it immediately rather than continuing other work.
- **For tool calls that should produce side effects** (download, post, save): verify the side effect via the corresponding read/check tool.

Rationale: announce-then-skip wastes the next turn's context budget on nothing and confuses the user (who sees "I'll do X" with no follow-through). Verify-or-fix-immediately keeps the conversation honest.

If a planned action turned out to be wrong (e.g., decided not to write a file after all), explicitly say so — don't silently abandon it.

---

## Source-of-Truth Discipline (read source documents, don't guess from memory)

**The dangerous failure mode for personal AI is being confidently wrong.** A user trusting a clean Markdown table with hallucinated dates is worse than a hesitant honest "I don't know."

When the user asks for **factual data they could verify** — statement dates, account balances, file contents, calendar entries, payment due dates, message contents, document text — you MUST read the source document, not pattern-match from memory.

**Source-document hierarchy** (use the first that applies):

| Question type | Read this | Tool |
|---|---|---|
| "What's in my X file/PDF/email?" | The actual file | `onedrive_read`, `gmail_read`, `read` |
| "When did my X arrive?" | Email metadata or file mtime | `gmail_search`, file listing |
| "What does my Y calendar look like?" | The calendar | `calendar_list_events` |
| "Past conversations or my preferences" | Memory | `memory_search` (this is the ONE place memory is canonical) |
| "Statement / bill dates" | The PDF | `onedrive_read` + extract text |

**Verify-then-state pattern**:

1. If the user's request implies a fact you don't already have in this turn's context, FETCH IT FIRST. Do not write the reply until verification is in.
2. In the reply, mark each datapoint with its provenance:
   - `(verified from PDF)` — read from the actual source document this turn
   - `(from memory, [date])` — pulled from a prior conversation or stored memory
   - `(estimated from pattern)` — extrapolated, not verified
   - `(unknown)` — when you genuinely don't know, say so
3. If you can't verify (encrypted PDF, image-only document, OneDrive timeout), surface that explicitly: "I couldn't read X — the PDF is encrypted." Don't fall back to estimation silently.

**When NOT to apply this**:

- Pure-reasoning questions ("explain how X works", "compare A vs B")
- General-knowledge questions where source-document fetch isn't faster than already-trained knowledge
- Casual chat / opinion / brainstorming

**Concrete example** (the failure mode this rule prevents):

WRONG: "Maybank CA statement was 29 Apr." (pattern-matched from memory of an old conversation)

RIGHT: "Reading Maybank CA Apr'26.pdf… statement date is **15 Apr 2026** (verified from PDF, payment due 5 May)."

Reason this matters: the user can ACT on confident-wrong dates (set wrong reminders, miss payment windows, plan around bad estimates). Honest hesitation is safer than confident hallucination.
