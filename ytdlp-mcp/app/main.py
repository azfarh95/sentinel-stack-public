import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import database as db
from . import downloader
# Importing recorder_bridge eagerly forces live_downloader to import too,
# which triggers the plugin auto-loader at module bottom — public plugin
# tier ships empty, drop *.py files into app/plugins/ to extend.
from .recorder_bridge import bridge as _live_bridge

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_MAX_BYTES = 50 * 1024 * 1024  # 50 MB — Telegram Bot API hard limit


# Pass DB init as FastMCP's lifespan so it runs inside the session manager's startup sequence
@asynccontextmanager
async def _lifespan(server: FastMCP):
    await db.init_db()
    asyncio.create_task(downloader.start_cleanup_loop())
    yield


mcp = FastMCP(
    "VideoDownloader",
    lifespan=_lifespan,
    instructions=(
        "Download videos from YouTube, TikTok, Instagram, Telegram, and 1000+ other sites. "
        "Typical flow: call video_info() first to confirm the video, then download_video() "
        "to start the download, then poll check_download() until status is 'done', "
        "then call send_to_telegram() to deliver the file back to the chat."
    ),
    # Allow Docker-internal hostnames in addition to the default localhost variants
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[
            "127.0.0.1:*", "localhost:*", "[::1]:*",
            "host.docker.internal:*", "ytdlp-mcp:*",
        ],
        allowed_origins=[
            "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*",
            "http://host.docker.internal:*", "http://ytdlp-mcp:*",
        ],
    ),
)


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
async def video_info(url: str) -> dict:
    """
    Get metadata for a video URL without downloading it.
    Returns title, uploader, duration, upload date, view count, thumbnail URL, and description snippet.
    Use this before download_video() to confirm you have the right video.
    """
    try:
        return await downloader.fetch_info(url)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def list_formats(url: str) -> list:
    """
    List all available quality and format combinations for a video URL.
    Shows format_id, resolution, fps, codec, and estimated file size.
    Useful when the user wants a specific format or when default 1080p is unavailable.
    """
    try:
        return await downloader.fetch_formats(url)
    except Exception as e:
        return [{"error": str(e)}]


@mcp.tool()
async def download_video(
    url: str,
    quality: str = "1080p",
    format: str = "mp4",
    filename: str = None,
    cookies_file: str = None,
    chat_id: str = None,
) -> dict:
    """
    Start downloading a video. Returns a job_id immediately — the download runs in the background.
    Use check_download(job_id) to track progress. File is saved to G:/YT-DLP on the host.

    If this URL was downloaded before and the file still exists, returns instantly from cache
    (no re-download). Pass cached=True jobs straight to send_to_telegram if needed.

    url:          The video URL (YouTube, TikTok, Instagram, Telegram, and 1000+ other sites)
    quality:      "1080p" | "720p" | "480p" | "best" | "audio-only"  (default: 1080p)
    format:       "mp4" | "webm" | "mp3" | "m4a"                     (default: mp4)
    filename:     Optional custom output filename without extension
    cookies_file: Optional cookie file stem to use for auth (e.g. "tiktok", "instagram").
                  Auto-detected from URL domain when omitted.
    chat_id:      Telegram chat ID to auto-send to when done (only active if telegram_mode
                  is "download+send"). Pass the chat_id from the incoming message.
    """
    try:
        result = await downloader.enqueue(
            url, quality=quality, fmt=format,
            custom_filename=filename, cookies_file=cookies_file,
            chat_id=chat_id,
        )
        job_id = result["job_id"]
        cached = result.get("cached", False)
        if cached:
            return {
                "job_id": job_id,
                "status": "done",
                "cached": True,
                "message": "Already downloaded. Call check_download(job_id) for file details.",
            }
        return {
            "job_id": job_id,
            "status": "queued",
            "cached": False,
            "message": f"Download queued. Call check_download('{job_id}') to track progress.",
        }
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
async def set_telegram_mode(mode: str) -> dict:
    """
    Toggle between download-only and auto-send mode.

    mode: "download"      — download files to G:/YT-DLP only, no auto-send (default)
          "download+send" — download then automatically send the file to Telegram

    Requires TELEGRAM_BOT_TOKEN env var to be set for "download+send" to work.
    The chat_id must also be passed to download_video() so the bot knows where to send.
    """
    mode = mode.strip().lower()
    if mode not in ("download", "download+send"):
        return {"error": "Invalid mode. Use 'download' or 'download+send'."}
    await db.set_setting("telegram_mode", mode)
    return {
        "ok": True,
        "telegram_mode": mode,
        "message": (
            "Auto-send enabled. Pass chat_id to download_video() to send files automatically."
            if mode == "download+send"
            else "Download-only mode. Use send_to_telegram() to send files manually."
        ),
    }


@mcp.tool()
async def get_telegram_mode() -> dict:
    """Get the current telegram mode setting: 'download' or 'download+send'."""
    mode = await db.get_setting("telegram_mode", "download")
    return {"telegram_mode": mode}


@mcp.tool()
async def check_download(job_id: str) -> dict:
    """
    Check the status of a download job.
    Status values: queued | downloading | done | error
    When downloading: shows progress %, speed, and estimated time remaining.
    When done: shows filename and file path on the host machine.
    """
    job = await downloader.get_job(job_id)
    if not job:
        return {"error": f"No job found with id '{job_id}'"}
    return job


@mcp.tool()
async def list_downloads(limit: int = 20) -> list:
    """
    List recent download jobs, most recent first.
    Shows job_id, URL, quality, status, filename, and timestamps.
    limit: number of results to return (default 20, max 100)
    """
    return await db.get_recent_jobs(min(limit, 100))


@mcp.tool()
async def send_to_telegram(
    chat_id: str,
    filepath: str,
    caption: str = None,
    bot_token: str = None,
) -> dict:
    """
    Send a downloaded file to a Telegram chat via the bot.

    chat_id:   Telegram chat ID to send to (user or group). Use the chat_id from the
               incoming message so the file goes back to whoever requested it.
    filepath:  Container path returned by check_download (e.g. /downloads/video.mp4)
    caption:   Optional caption shown under the media (e.g. video title)
    bot_token: Optional override; defaults to the TELEGRAM_BOT_TOKEN env var.

    Telegram limit: 50 MB. Files larger than this cannot be sent via the bot API —
    the tool will return an error with the local filepath so you can inform the user.
    """
    token = bot_token or TELEGRAM_BOT_TOKEN
    if not token:
        return {"error": "No bot token configured. Set TELEGRAM_BOT_TOKEN env var or pass bot_token parameter."}

    path = Path(filepath)
    if not path.exists():
        return {"error": f"File not found: {filepath}"}

    size = path.stat().st_size
    if size > TELEGRAM_MAX_BYTES:
        size_mb = round(size / 1024 / 1024, 1)
        return {
            "error": "file_too_large",
            "size_mb": size_mb,
            "message": (
                f"File is {size_mb} MB — exceeds Telegram's 50 MB bot API limit. "
                f"The file is saved locally at {filepath} (G:/YT-DLP/ on Windows)."
            ),
        }

    ext = path.suffix.lower()
    if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
        method = "sendVideo"
        field = "video"
    elif ext in (".mp3", ".m4a", ".ogg", ".flac", ".wav"):
        method = "sendAudio"
        field = "audio"
    else:
        method = "sendDocument"
        field = "document"

    api_url = f"https://api.telegram.org/bot{token}/{method}"

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            with open(filepath, "rb") as f:
                data = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption
                response = await client.post(api_url, data=data, files={field: (path.name, f)})
        result = response.json()
    except Exception as e:
        return {"error": f"HTTP error: {str(e)}"}

    if result.get("ok"):
        return {
            "ok": True,
            "message_id": result["result"]["message_id"],
            "filename": path.name,
            "size_mb": round(size / 1024 / 1024, 1),
        }
    return {"error": result.get("description", "Telegram API error"), "response": result}


# ── Livestream-recording tools (V1 port from SMDL standalone) ─────────────────
# Async-running, long-lived recordings. The MCP caller (agent) initiates with
# record_live_start, polls progress with record_live_status, and stops with
# record_live_stop. Session keying: agent provides a string session_id (or
# we auto-generate); same id is used across all four tools.

import time as _time
import asyncio as _asyncio

# session_id (string) → int chat_id used internally by bridge. Agents work
# in string-typed identifiers; bridge keys by int.
_live_sessions: dict[str, dict] = {}

def _session_to_int(session_id: str) -> int:
    """Stable hash from string session_id to the int the bridge expects."""
    # CRC32 fits in int and is collision-rare-enough for the tiny session set
    # an agent maintains at any one moment. Bridge stores under abs() so
    # negative ints are fine but we keep positive for cleaner logs.
    import zlib
    return zlib.crc32(session_id.encode()) & 0x7FFFFFFF


@mcp.tool()
async def record_live_start(
    url: str,
    session_id: str = None,
    quality_height: int = None,
    transcode_height: int = 0,
    transcode_keep_original: bool = False,
    cookies_file: str = None,
) -> dict:
    """
    Start recording a live HLS stream (YouTube live, Twitch, Kick, and any other
    yt-dlp-supported live URL). Runs as a background task — call record_live_status
    to poll progress, record_live_stop to halt gracefully.

    Cloudflare-protected sites (those listed in installed plugins, e.g. cam-site
    plugins) get Chrome TLS impersonation automatically.

    url:                       Live-stream URL.
    session_id:                Caller-chosen string id; reused across status/stop.
                               Auto-generated if omitted; returned in the result.
    quality_height:            Optional resolution cap (e.g. 720). Overrides the
                               LIVE_MAX_HEIGHT default. 0 = source/unlimited.
    transcode_height:          Optional post-recording re-encode (480 or 240).
                               0 = no transcode.
    transcode_keep_original:   If transcoding, keep the original file too.
    cookies_file:              Optional cookie file stem (e.g. "twitch") for
                               sub-only / age-gated streams. Auto-resolves to
                               /cookies/<stem>.txt.

    Returns: {session_id, job_id, started_at, status} or {error}.
    """
    from .live_downloader import _resolve_cookies, LIVE_MAX_HEIGHT  # lazy import
    from . import live_downloader as _ld

    sid = session_id or f"mcp-{uuid.uuid4().hex[:8]}"
    chat_int = _session_to_int(sid)

    if _live_bridge.has_job(chat_int):
        existing = _live_bridge.status(chat_int)
        return {
            "error": "already_recording",
            "session_id": sid,
            "message": f"Session '{sid}' already has an active recording.",
            "elapsed_seconds": existing.elapsed_seconds if existing else 0,
        }

    cookiepath = None
    if cookies_file:
        cookiepath = f"/cookies/{cookies_file}.txt"
        if not Path(cookiepath).exists():
            cookiepath = None
    if not cookiepath:
        cookiepath = downloader._resolve_cookies(url, None)

    # Allow per-call override of LIVE_MAX_HEIGHT by temporarily patching the
    # module constant — bridge passes everything through to record_live().
    # Cleaner alternative would be plumbing a height kwarg through bridge.record;
    # leaving that as a V1.1 follow-up.
    if quality_height is not None:
        _ld.LIVE_MAX_HEIGHT = int(quality_height)

    # Fire-and-forget — record() blocks for hours; agent polls status separately.
    task = _asyncio.create_task(_live_bridge.record(
        chat_int, url,
        cookiepath=cookiepath,
        transcode_height=transcode_height,
        transcode_keep_original=transcode_keep_original,
    ))
    _live_sessions[sid] = {"chat_int": chat_int, "task": task, "started_at": _time.time()}

    # Give the bridge a moment to register the job in its internal dict
    await _asyncio.sleep(0.2)
    status = _live_bridge.status(chat_int)
    return {
        "session_id": sid,
        "started_at": _live_sessions[sid]["started_at"],
        "status":     "recording" if status else "starting",
        "message":    "Recording started. Poll record_live_status to track, record_live_stop to halt.",
    }


@mcp.tool()
async def record_live_status(session_id: str) -> dict:
    """
    Get the current status of a recording session. Returns elapsed time, bytes
    downloaded so far, and abort_reason if the recording has ended.

    session_id: The id returned by record_live_start (or one the agent supplied).
    """
    sess = _live_sessions.get(session_id)
    if not sess:
        return {"error": "no_session", "session_id": session_id}
    chat_int = sess["chat_int"]
    status = _live_bridge.status(chat_int)
    task = sess["task"]
    if status is None:
        # Job ended — return the task's result if available
        if task.done():
            try:
                result = task.result()
                return {
                    "session_id": session_id,
                    "status": "ended",
                    "abort_reason":      result.get("abort_reason"),
                    "duration_seconds":  result.get("duration_seconds"),
                    "bytes_downloaded":  result.get("bytes_downloaded"),
                    "files":             result.get("files", []),
                    "platform":          result.get("platform"),
                    "detail":            result.get("detail"),
                }
            except Exception as e:
                return {"session_id": session_id, "status": "ended", "error": str(e)}
        return {"session_id": session_id, "status": "ended_no_result"}
    return {
        "session_id":      session_id,
        "status":          "recording",
        "elapsed_seconds": status.elapsed_seconds,
        "bytes":           status.bytes,
        "filepath":        status.filepath,
        "platform":        status.platform,
        "uploader":        status.uploader,
    }


@mcp.tool()
async def record_live_stop(session_id: str) -> dict:
    """
    Request a graceful stop on a recording session. Sets the stop flag; the
    watchdog terminates ffmpeg within ~15-30 s if it doesn't exit cleanly on
    its own. Returns a status snapshot taken at the moment of the stop request
    — call record_live_status afterwards to confirm the recording has ended
    and to retrieve the final file path.

    session_id: The id used in record_live_start.
    """
    sess = _live_sessions.get(session_id)
    if not sess:
        return {"error": "no_session", "session_id": session_id}
    chat_int = sess["chat_int"]
    status = await _live_bridge.stop(chat_int)
    if status is None:
        return {"session_id": session_id, "status": "no_active_job"}
    return {
        "session_id":      session_id,
        "status":          "stopping",
        "elapsed_seconds": status.elapsed_seconds,
        "bytes":           status.bytes,
        "filepath":        status.filepath,
        "message":         "Stop requested. Call record_live_status in ~30 s to confirm completion + get files.",
    }


@mcp.tool()
async def record_live_list_active() -> list:
    """
    List all currently-running recording sessions in this MCP process.
    Returns the metadata each session was started with plus current elapsed +
    bytes downloaded.
    """
    out = []
    for sid, sess in list(_live_sessions.items()):
        status = _live_bridge.status(sess["chat_int"])
        if status is None:
            continue  # ended; skip
        out.append({
            "session_id":      sid,
            "elapsed_seconds": status.elapsed_seconds,
            "bytes":           status.bytes,
            "filepath":        status.filepath,
            "platform":        status.platform,
            "uploader":        status.uploader,
        })
    return out


# ── REST API (used by sm-dl and other non-MCP callers) ────────────────────────

async def _api_identify(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    url = body.get("url", "").strip()
    if not url:
        return JSONResponse({"error": "Missing url"}, status_code=400)
    try:
        result = await downloader.identify_post(url)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def _api_start_download(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    url = body.get("url", "").strip()
    if not url:
        return JSONResponse({"error": "Missing url"}, status_code=400)
    quality = body.get("quality", "1080p")
    cookies_file = body.get("cookies_file")
    media_type = body.get("media_type")
    temp = bool(body.get("temp", False))
    try:
        result = await downloader.enqueue(url, quality=quality, cookies_file=cookies_file, media_type=media_type, temp=temp)
        job_id = result["job_id"]
        cached = result.get("cached", False)
        return JSONResponse({"job_id": job_id, "status": "done" if cached else "queued", "cached": cached})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def _api_check_download(request: Request) -> JSONResponse:
    job_id = request.path_params.get("job_id", "")
    job = await downloader.get_job(job_id)
    if not job:
        return JSONResponse({"error": f"No job found: {job_id}"}, status_code=404)
    return JSONResponse(job)


# ── Build the ASGI app ─────────────────────────────────────────────────────────

async def _health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "ytdlp-mcp"})


app = mcp.streamable_http_app()
app.router.routes.insert(0, Route("/api/download/{job_id}", _api_check_download, methods=["GET"]))
app.router.routes.insert(0, Route("/api/download", _api_start_download, methods=["POST"]))
app.router.routes.insert(0, Route("/api/identify", _api_identify, methods=["POST"]))
app.router.routes.insert(0, Route("/health", _health, methods=["GET"]))
