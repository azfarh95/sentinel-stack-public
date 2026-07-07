# SMDL Livestream V1 — Smoke Test Report (BLOCKED)

**Date:** 10 May 2026
**Tester:** Claude (autonomous)
**Target:** SMDL standalone container (port 8096), `/stop_livestream` flow, platform whitelist
**Spec:** `workspace/proposals/2026-05-10-SMDL-Livestreams.md`

---

## TL;DR — Test Could Not Run

The livestream V1 code is **on disk** but **not in the running container**. No URL was sent to @YourSMDLBot; no recording was attempted. The smoke test is blocked on a deploy step that wasn't executed.

| Platform | Status | Reason |
|---|---|---|
| Twitch | **SKIPPED** | Container missing live code |
| Instagram | **SKIPPED** | Container missing live code |
| TikTok | **SKIPPED** | Container missing live code |

---

## Root cause

The smdl container is running an image built on **2026-05-04 07:27:28 UTC**. The livestream V1 source files (`smdl/app/live_downloader.py`, the rewritten `smdl/app/bot.py`, the new `smdl/app/config.py` keys) were edited on the host on **2026-05-10**. The image has not been rebuilt since.

Evidence:

```
Container image:   sha256:a06df59…   created 2026-05-04T07:27:28Z
Container started: 2026-05-07T23:53Z

# Inside the running container:
docker exec smdl ls /app/app/
  bot.py         (4587 bytes, May 4)         # OLD — pre-live
  config.py      (1746 bytes, May 4)         # OLD — pre-live
  downloader.py  (12428 bytes, May 4)        # OLD
  (no live_downloader.py)

# On the host:
smdl/app/bot.py             (10698 bytes, May 10)        # NEW — live-aware
smdl/app/config.py          (2722 bytes, May 10)         # NEW
smdl/app/live_downloader.py (10497 bytes, May 10)        # NEW (only on host)
```

The compose file uses `build:` (no bind-mount on `/app`), so changes to host code are only picked up at image rebuild time:

```yaml
smdl:
  build:
    context: ./smdl
    dockerfile: Dockerfile
  volumes:
    - …:/downloads
    - "./smdl/config:/config"   # only config is bind-mounted
    - smdl_data:/data
```

Confirmation that the running container can't even read the new config keys:

```
docker exec smdl python -c "from app.config import LIVE_ENABLED"
> ImportError: cannot import name 'LIVE_ENABLED' from 'app.config'
> Unknown config keys (ignored): live_abort_on_session_fail, live_enabled,
>   live_heartbeat_seconds, live_max_concurrent, live_min_free_disk_gb,
>   live_platforms
```

The new keys are present in the bind-mounted `/config/smdl.json` (`live_enabled: true`, `live_platforms: ["youtube","twitch","kick"]`, etc.) but the container's pre-live `config.py` doesn't know about them and logs them as "unknown — ignored".

---

## Test infrastructure status

`claude-assistant-testbot` and `claude-assistant-testlogger` are **healthy** (up 25 hours). However, there is a **second blocker** even after the container is rebuilt:

- The Sentinel test group `Sentinel Test envrionment` (chat ID `-5116301620`) currently has **2 members**: azfar (admin) and the testbot. **`@YourSMDLBot` is not a member.**
- The testbot's reply-monitor is also hard-coded to `ECHO_BOT_ID=7552648476` (the OpenClaw echo bot in `bot.py`), so even if the nanobot were added, the testbot would silently drop its replies.

To test the nanobot via the existing testbot infrastructure, two changes are needed:

1. Add @YourSMDLBot to the test group (one-time, manual via Telegram UI).
2. Either:
   - Extend `testbot/app/bot.py` to also forward messages from the nanobot's user ID, **or**
   - Drive the test from azfar's Telethon session directly (DM @YourSMDLBot from the user account; bypasses the bot↔bot visibility limitation entirely).

Neither change was attempted because the deploy is blocked anyway and the constraints forbid touching credentials/configs.

---

## Per-platform readiness (static analysis from the host code)

I read the V1 source on the host and confirmed the logic for each test target — these would be the **expected** outcomes once the container is rebuilt and the bot can receive a URL:

### Twitch

`live_downloader._platform_allowed("https://www.twitch.tv/twitch")` returns `(True, "twitch")`.

Expected flow on a live URL:
1. `bot.py handle_message` → `identify_post(url)` (existing yt-dlp path) → `is_live=True`.
2. Routes to `record_live(url, cookiepath, on_progress=…, stop_flag={…})`.
3. Status message edits to `🔴 Recording · @<channel> · 0 min · 0 MB · still live`, refreshed every 300 s (`LIVE_HEARTBEAT_SECONDS=300`).
4. After ~120 s, `/stop_livestream` from the same chat sets `stop_flag["stop"]=True`. The yt-dlp `progress_hook` checks the flag on next chunk and raises `LiveAbort("user_stopped")`. `bot.py` finalizes with `⏹ Stopped by /stop_livestream · N min · X MB saved`.

Risk: with `live_heartbeat_seconds=300` and a 120-s test, the user would see no heartbeat update during the test window — only the initial "Recording started" line and the final "Stopped" line. That's expected, not a bug, but flagging because it makes the smoke test feel quiet.

### Instagram (intentional reject path)

URL `https://www.instagram.com/<account>/live/`. Two possible failure points before `record_live` is even reached:

- `identify_post()` calls yt-dlp's extractor; for IG `/live/` URLs without a live broadcast, it returns an `error` and `bot.py` shows `Could not identify post: <error>` with the IG error truncated to 200 chars.
- If yt-dlp does mark it live, control reaches `record_live`, which calls `_platform_allowed` → `("instagram" not in LIVE_PLATFORMS, "instagram")` → returns `abort_reason="platform_not_allowed"` with detail `Live recording disabled for instagram. Whitelist: ['kick','twitch','youtube'].` The bot edits the status to `⚠ Live recording disabled for instagram. Whitelist: …`.

Both paths are acceptable rejections. No partial file is written because `_platform_allowed` is checked **before** `Path(LIVE_DIR).mkdir(...)`.

### TikTok (intentional reject path)

Same shape as Instagram. `_platform_allowed("https://www.tiktok.com/@<acct>/live")` returns `(False, "tiktok")` → `abort_reason="platform_not_allowed"`. Note: without a live broadcast at the URL, yt-dlp will likely fail at `identify_post()` first with a generic extractor error — the user-facing message will be `Could not identify post: …` rather than the cleaner `platform_not_allowed`. That's an upstream yt-dlp behaviour, not an SMDL bug, but it means the **`platform_not_allowed` clean-reject path is hard to trigger from the user side** unless yt-dlp manages to identify the URL as live first.

---

## Stability assessment

N/A — no recording ran. The bot stayed responsive on its existing (non-live) message handler throughout the diagnostic period.

---

## Patterns worth flagging (from code review, not from runtime)

1. **`asyncio.run_coroutine_threadsafe(...)` in `_maybe_emit`** — the heartbeat is dispatched from the yt-dlp progress hook, which runs in a thread (`run_in_executor`). The code calls `asyncio.get_event_loop()` inside the thread, which on Python 3.10+ raises `DeprecationWarning` and on 3.12+ may return a fresh non-running loop, making the `run_coroutine_threadsafe` a no-op. The bare `except RuntimeError: pass` swallows it. The fix is to capture `loop = asyncio.get_running_loop()` at the top of `record_live` (where it's already done — line 244 — but *not passed* into the closure) and reuse that captured `loop` reference inside `_maybe_emit`. As written, the heartbeat is **likely silent**, which fits the "quiet test" risk above.

2. **`partial files` discovery on abort** — when `state["filepath"]` is unset (most aborts) the code globs `LIVE_DIR/**/*.mp4` for files newer than `started_at`. yt-dlp writes `*.f<format>.mp4.part` during recording and only renames to `.mp4` on natural completion. So an aborted recording will leave **`.part` files** that the glob ignores → `files=[]` → user gets "0 MB saved" messaging even when bytes were captured. Worth either globbing `*.mp4*` and renaming `.part` to `.mp4` on user-stop, or accepting this as "saves only on clean stream end". (User cleanup command `find /downloads/live -name "*.part" -delete` is correct for this.)

3. **`live_heartbeat_seconds=300`** is very long for short test recordings. Consider exposing a CLI/config override, or auto-shortening the first heartbeat to 30s.

---

## Recommendation

**Do not promote V1 to SMDL MCP yet.** This run covered **0 of the 4** promotion-gate conditions because the feature isn't actually running. The conditions are:

| # | Gate condition | Covered by this run? | Status |
|---|---|---|---|
| 1 | One full successful recording per platform (YT/Twitch/Kick) | **No** | Pending — needs container rebuild + a live URL |
| 2 | Intentional session-fail test with clean abort message | **No** | Pending — needs container rebuild + cookie expiry simulation |
| 3 | Disk-low test fires `disk_low` before any data is written | **No** | Pending — needs container rebuild + disk fill |
| 4 | No regression in regular (non-live) download flow | **Partial** | The currently-running container is the pre-live version, which by definition still works for non-live downloads. After rebuild, this gate needs re-verification. |

### Next steps to unblock

1. Rebuild and restart the smdl container:
   ```powershell
   docker compose build smdl
   docker compose up -d smdl
   docker exec smdl python -c "from app.config import LIVE_ENABLED, LIVE_PLATFORMS; print(LIVE_ENABLED, sorted(LIVE_PLATFORMS))"
   # expect:  True ['kick', 'twitch', 'youtube']
   ```
2. Decide on the test surface (one of):
   - **Easiest:** azfar DMs @YourSMDLBot directly from his account with a live Twitch URL and watches the status message. No infra changes.
   - **Automatable:** add @YourSMDLBot to `Sentinel Test envrionment`, extend testbot to also relay nanobot messages (or drive via Telethon DM from azfar's session).
3. Re-run this smoke test once the rebuild is verified. Twitch live channels rotate fast — pick at the moment of testing.
4. Address the heartbeat-loop bug (item 1 above) in the same iteration, since it'll be invisibly broken otherwise.

---

## Cleanup

`/downloads/live/` does not exist in the container — confirmed with `docker exec smdl ls /downloads/live` → `No such file or directory`. No partial files to remove. The directory is created lazily by `record_live` on first use.
