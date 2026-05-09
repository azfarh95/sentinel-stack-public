import asyncio
import os
import re
import secrets
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yt_dlp

from . import database as db

DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/downloads")
TEMP_DIR      = os.path.join(DOWNLOADS_DIR, "temp")   # guest/other-user downloads
COOKIES_DIR   = os.environ.get("COOKIES_DIR", "/cookies")
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "2"))
DEFAULT_QUALITY = os.environ.get("DEFAULT_QUALITY", "1080p")
TEMP_TTL_HOURS  = int(os.environ.get("TEMP_TTL_HOURS", "24"))

_semaphore: asyncio.Semaphore | None = None
_live: dict[str, dict] = {}  # in-process progress cache (reset on restart, DB is source of truth)

# Maps URL domain fragments → cookie filename stem (without .txt)
_SITE_COOKIE_MAP = {
    "tiktok.com": "tiktok",
    "instagram.com": "instagram",
    "x.com": "twitter",
    "twitter.com": "twitter",
    "facebook.com": "facebook",
    "fb.com": "facebook",
}


def _platform_from_url(url: str) -> str:
    u = url.lower()
    if "instagram.com" in u:   return "Instagram"
    if "tiktok.com" in u:      return "TikTok"
    if "twitter.com" in u or "x.com" in u: return "Twitter"
    if "facebook.com" in u or "fb.com" in u: return "Facebook"
    if "youtube.com" in u or "youtu.be" in u: return "Youtube"
    if "reddit.com" in u:      return "Reddit"
    return "Unknown"


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    return _semaphore


def _quality_to_format(quality: str) -> str:
    q = quality.lower().strip()
    if q in ("best", ""):
        return "bestvideo+bestaudio/best"
    if q == "audio-only":
        return "bestaudio[ext=m4a]/bestaudio/best"
    height = q.rstrip("p")
    try:
        h = int(height)
        return (
            f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={h}]+bestaudio"
            f"/best[height<={h}]/best"
        )
    except ValueError:
        return "bestvideo[height<=1080]+bestaudio/best"


def _resolve_cookies(url: str, cookies_file: str | None) -> str | None:
    """Return cookiefile path if it exists, else None.

    If cookies_file is given (e.g. "tiktok"), look for /cookies/tiktok.txt.
    If omitted, auto-detect by URL domain from _SITE_COOKIE_MAP.
    """
    name = cookies_file
    if not name:
        url_lower = url.lower()
        for domain, cookie_name in _SITE_COOKIE_MAP.items():
            if domain in url_lower:
                name = cookie_name
                break
    if not name:
        return None
    path = Path(COOKIES_DIR) / f"{name}.txt"
    return str(path) if path.exists() else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _blank_job(job_id: str, url: str, quality: str, fmt: str | None) -> dict:
    return {
        "job_id": job_id, "url": url, "quality": quality, "fmt": fmt,
        "status": "queued", "progress": None, "speed": None, "eta": None,
        "filename": None, "filepath": None, "download_dir": None, "files": [],
        "error": None, "created_at": _now(), "completed_at": None,
    }


async def identify_post(url: str) -> dict:
    """Determine media type (video/photo/carousel) before downloading."""
    loop = asyncio.get_running_loop()
    cookiepath = _resolve_cookies(url, None)
    opts = {"quiet": True, "no_warnings": True}
    if cookiepath:
        opts["cookiefile"] = cookiepath

    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await loop.run_in_executor(None, _run)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "No video formats found" in msg:
            # yt-dlp raises this on photo/image posts even with download=False
            return {
                "platform": _platform_from_url(url),
                "uploader": None, "uploader_id": None,
                "media_type": "photo", "count": 1,
                "is_private": False, "title": None, "thumbnail": None,
            }
        is_private = any(k in msg.lower() for k in ("private", "login", "sign in", "restricted"))
        return {"error": msg[:300], "is_private": is_private}
    except Exception as e:
        return {"error": str(e)[:300], "is_private": False}

    entries = list(info.get("entries") or [])
    if entries or info.get("_type") == "playlist":
        media_type = "carousel"
        count = len(entries)
    else:
        vcodec = info.get("vcodec") or ""
        ext = (info.get("ext") or "").lower()
        duration = info.get("duration") or 0
        if ext in ("jpg", "jpeg", "png", "webp") or vcodec in ("none", ""):
            media_type = "photo"
        else:
            media_type = "video"
        count = 1

    return {
        "platform": info.get("extractor_key", "unknown"),
        "uploader": info.get("uploader") or info.get("channel"),
        "uploader_id": info.get("uploader_id") or info.get("channel_id"),
        "media_type": media_type,
        "count": count,
        "is_private": False,
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
    }


async def fetch_info(url: str) -> dict:
    loop = asyncio.get_running_loop()

    def _run():
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            return ydl.extract_info(url, download=False)

    info = await loop.run_in_executor(None, _run)
    duration = info.get("duration")
    if duration:
        mins, secs = divmod(int(duration), 60)
        hours, mins = divmod(mins, 60)
        duration_str = f"{hours}:{mins:02d}:{secs:02d}" if hours else f"{mins}:{secs:02d}"
    else:
        duration_str = None

    return {
        "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": duration_str,
        "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"),
        "thumbnail": info.get("thumbnail"),
        "description": (info.get("description") or "")[:300],
        "webpage_url": info.get("webpage_url") or url,
        "extractor": info.get("extractor_key"),
    }


async def fetch_formats(url: str) -> list[dict]:
    loop = asyncio.get_running_loop()

    def _run():
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            info = ydl.extract_info(url, download=False)
            return info.get("formats", [])

    fmts = await loop.run_in_executor(None, _run)
    results = []
    for f in fmts:
        sz = f.get("filesize") or f.get("filesize_approx")
        results.append({
            "format_id": f.get("format_id"),
            "ext": f.get("ext"),
            "resolution": f.get("resolution") or f"{f.get('width', '?')}x{f.get('height', '?')}",
            "fps": f.get("fps"),
            "filesize_mb": round(sz / 1024 / 1024, 1) if sz else None,
            "vcodec": f.get("vcodec"),
            "acodec": f.get("acodec"),
            "note": f.get("format_note"),
        })
    return results


async def cleanup_temp_files() -> None:
    """Delete files in TEMP_DIR that are older than TEMP_TTL_HOURS hours."""
    import time
    temp_path = Path(TEMP_DIR)
    if not temp_path.exists():
        return
    cutoff = time.time() - TEMP_TTL_HOURS * 3600
    deleted = 0
    for f in temp_path.rglob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                deleted += 1
            except Exception:
                pass
    # Remove empty subdirs
    for d in sorted(temp_path.rglob("*"), reverse=True):
        if d.is_dir():
            try:
                d.rmdir()
            except OSError:
                pass


async def start_cleanup_loop() -> None:
    """Background loop: run cleanup every hour."""
    while True:
        await asyncio.sleep(3600)
        try:
            await cleanup_temp_files()
        except Exception:
            pass


async def enqueue(
    url: str,
    quality: str | None = None,
    fmt: str | None = None,
    custom_filename: str | None = None,
    cookies_file: str | None = None,
    media_type: str | None = None,
    chat_id: str | None = None,
    temp: bool = False,
) -> dict:
    """Queue a download. Returns dict with job_id and cached=True if already downloaded."""
    quality = (quality or DEFAULT_QUALITY).strip()

    # ── Cache check (owner downloads only) ───────────────────────────────────
    cached = None if temp else await db.get_url_cache(url)
    if cached:
        # Files exist on disk — return a synthetic "done" job without re-downloading
        job_id = secrets.token_hex(4)
        files = cached["files"]
        filepath = files[0] if files else None
        filename = Path(filepath).name if filepath else None
        download_dir = str(Path(filepath).parent) if filepath else None
        job = {
            **_blank_job(job_id, url, quality, fmt),
            "status": "done",
            "progress": "100%",
            "filename": filename,
            "filepath": filepath,
            "download_dir": download_dir,
            "files": files,
            "completed_at": _now(),
            "cached": True,
        }
        _live[job_id] = job.copy()
        await db.upsert_job(job)
        # Auto-send if mode is download+send
        if chat_id:
            asyncio.create_task(_maybe_send_telegram(files, chat_id))
        return {"job_id": job_id, "cached": True}

    # ── Fresh download ────────────────────────────────────────────────────────
    job_id = secrets.token_hex(4)
    job = _blank_job(job_id, url, quality, fmt)
    _live[job_id] = job.copy()
    await db.upsert_job(job)
    asyncio.create_task(_run_download(job_id, url, quality, fmt, custom_filename, cookies_file, media_type, chat_id, temp))
    return {"job_id": job_id, "cached": False}


async def get_job(job_id: str) -> dict | None:
    # Live dict has real-time progress; DB has persisted state for completed jobs
    return _live.get(job_id) or await db.get_job(job_id)


async def _maybe_send_telegram(files: list[str], chat_id: str):
    """Send files to Telegram if telegram_mode == 'download+send'."""
    import httpx
    mode = await db.get_setting("telegram_mode", "download")
    if mode != "download+send":
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token or not chat_id:
        return
    for filepath in files:
        path = Path(filepath)
        if not path.exists():
            continue
        size = path.stat().st_size
        if size > 50 * 1024 * 1024:
            # File too large — notify user
            async with httpx.AsyncClient(timeout=30.0) as client:
                await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": f"⚠️ File too large to send (>{round(size/1024/1024)}MB): {path.name}\nSaved to: {filepath}"},
                )
            continue
        ext = path.suffix.lower()
        if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
            method, field = "sendVideo", "video"
        elif ext in (".mp3", ".m4a", ".ogg", ".flac", ".wav"):
            method, field = "sendAudio", "audio"
        else:
            method, field = "sendDocument", "document"
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                with open(filepath, "rb") as f:
                    await client.post(
                        f"https://api.telegram.org/bot{token}/{method}",
                        data={"chat_id": chat_id},
                        files={field: (path.name, f)},
                    )
        except Exception:
            pass


class _GalleryDLFallback(Exception):
    """Raised inside a thread to signal the async caller to retry with gallery-dl."""


def _gallery_dl_run(base_dir: str, url: str, cookiepath: str | None) -> list[str]:
    """Blocking: run gallery-dl into base_dir and return sorted list of newly downloaded files.

    gallery-dl creates its own subdirectory structure:
      {base_dir}/{platform}/{username}/{filename}
    e.g. /downloads/instagram/johndoe/2024-01-01_abc123.jpg
    """
    Path(base_dir).mkdir(parents=True, exist_ok=True)
    # Snapshot existing files so we only return files added by this job
    existing = {str(f) for f in Path(base_dir).rglob("*") if f.is_file()}
    cmd = ["gallery-dl", "-d", base_dir, url]
    if cookiepath:
        cmd += ["--cookies", cookiepath]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    all_files = sorted([
        str(f) for f in Path(base_dir).rglob("*")
        if f.is_file() and not f.name.startswith(".") and str(f) not in existing
    ])
    if not all_files:
        err = (proc.stderr or proc.stdout or "no files downloaded").strip()[:300]
        raise Exception(f"gallery-dl: {err}")
    return all_files


async def _run_download(
    job_id: str,
    url: str,
    quality: str,
    fmt: str | None,
    custom_filename: str | None,
    cookies_file: str | None,
    media_type: str | None,
    chat_id: str | None = None,
    temp: bool = False,
):
    async with _get_semaphore():
        _live[job_id]["status"] = "downloading"
        await db.upsert_job(_live[job_id])

        loop = asyncio.get_running_loop()
        cookiepath = _resolve_cookies(url, cookies_file)

        out_dir = TEMP_DIR if temp else DOWNLOADS_DIR
        Path(out_dir).mkdir(parents=True, exist_ok=True)

        try:
            if media_type in ("photo", "carousel"):
                # gallery-dl handles Instagram/TikTok images natively; yt-dlp cannot
                all_files = await loop.run_in_executor(
                    None, _gallery_dl_run, out_dir, url, cookiepath
                )
                filepath = all_files[0]
                filename = Path(filepath).name
                download_dir = str(Path(filepath).parent)
            else:
                audio_only = quality.lower() == "audio-only"
                merge_fmt = fmt or ("m4a" if audio_only else "mp4")
                fmt_str = _quality_to_format(quality)

                if custom_filename:
                    outtmpl = f"{out_dir}/{custom_filename}.%(ext)s"
                elif temp:
                    # Guests: flat dump, no subdirectory organisation
                    outtmpl = f"{out_dir}/%(title).80s.%(ext)s"
                elif audio_only:
                    outtmpl = f"{out_dir}/%(extractor)s/%(uploader,uploader_id)s/%(title).80s.%(ext)s"
                else:
                    outtmpl = f"{out_dir}/%(extractor)s/%(uploader,uploader_id)s/%(title).80s.%(ext)s"

                final: dict = {"path": None, "prepared": None}

                def hook(d):
                    if d["status"] == "downloading":
                        _live[job_id].update({
                            "progress": d.get("_percent_str", "").strip(),
                            "speed":    d.get("_speed_str", "").strip(),
                            "eta":      d.get("_eta_str", "").strip(),
                        })
                    elif d["status"] == "finished" and not final["path"]:
                        final["path"] = d.get("filename")

                ydl_opts = {
                    "format": fmt_str,
                    "outtmpl": outtmpl,
                    "merge_output_format": merge_fmt,
                    "quiet": True,
                    "no_warnings": True,
                    "progress_hooks": [hook],
                    "postprocessors": [{"key": "FFmpegMetadata"}],
                }
                if cookiepath:
                    ydl_opts["cookiefile"] = cookiepath

                def _run():
                    def _extract(opts):
                        with yt_dlp.YoutubeDL(opts) as ydl:
                            info = ydl.extract_info(url, download=True)
                            final["prepared"] = ydl.prepare_filename(info)
                            if not final["path"]:
                                final["path"] = final["prepared"]
                            return info
                    try:
                        return _extract(ydl_opts)
                    except yt_dlp.utils.DownloadError as e:
                        if "No video formats found" not in str(e):
                            raise
                        # Unknown media type that turned out to be an image — try gallery-dl
                        raise _GalleryDLFallback()

                try:
                    await loop.run_in_executor(None, _run)
                except _GalleryDLFallback:
                    all_files = await loop.run_in_executor(
                        None, _gallery_dl_run, out_dir, url, cookiepath
                    )
                    filepath = all_files[0]
                    filename = Path(filepath).name
                    download_dir = str(Path(filepath).parent)
                    _live[job_id].update({
                        "status": "done", "progress": "100%",
                        "speed": None, "eta": None,
                        "filename": filename, "filepath": filepath,
                        "download_dir": download_dir, "files": all_files,
                        "completed_at": _now(),
                    })
                    await db.upsert_job(_live[job_id])
                    return

                raw = Path(final["path"] or "")
                filepath = None
                clean_stem = re.sub(r"\.f\d+$", "", raw.stem)
                merged = raw.parent / f"{clean_stem}.{merge_fmt}"
                if merged.exists():
                    filepath = str(merged)
                if not filepath:
                    prepared = Path(final.get("prepared") or "")
                    filepath = str(prepared) if prepared.exists() else str(raw)

                filename = Path(filepath).name
                download_dir = str(Path(filepath).parent)
                all_files = [filepath]

            _live[job_id].update({
                "status": "done", "progress": "100%",
                "speed": None, "eta": None,
                "filename": filename, "filepath": filepath,
                "download_dir": download_dir, "files": all_files,
                "completed_at": _now(),
            })
            # Save to URL cache (owner downloads only — temp files expire anyway)
            if not temp:
                platform = _platform_from_url(url)
                uploader = Path(filepath).parent.name if filepath else None
                await db.set_url_cache(url, all_files, platform, uploader)
            # Auto-send to Telegram if mode is download+send
            if chat_id:
                asyncio.create_task(_maybe_send_telegram(all_files, chat_id))

        except Exception as exc:
            _live[job_id].update({"status": "error", "error": str(exc)[:500]})

        await db.upsert_job(_live[job_id])
