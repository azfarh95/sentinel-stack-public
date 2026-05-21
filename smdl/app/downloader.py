"""Direct yt-dlp + gallery-dl downloader — no external API dependency."""

import asyncio
import os
import re
import subprocess
from pathlib import Path

import yt_dlp
from telegram import InputMediaPhoto

from . import database as db
from .config import DEFAULT_QUALITY, DELETE_AFTER_SEND, MAX_CONCURRENT, TEMP_TTL_HOURS

DOWNLOADS_DIR      = os.environ.get("DOWNLOADS_DIR", "/downloads")
TEMP_DIR           = os.path.join(DOWNLOADS_DIR, "temp")
COOKIES_DIR        = os.environ.get("COOKIES_DIR", "/cookies")
TELEGRAM_MAX_BYTES = 50 * 1024 * 1024

_semaphore: asyncio.Semaphore | None = None

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
_ANIM_EXTS  = {".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
_AUDIO_EXTS = {".mp3", ".m4a", ".ogg", ".flac", ".wav"}

_SITE_COOKIE_MAP = {
    "tiktok.com":    "tiktok",
    "instagram.com": "instagram",
    "x.com":         "twitter",
    "twitter.com":   "twitter",
    "facebook.com":  "facebook",
    "fb.com":        "facebook",
    "twitch.tv":     "twitch",
    "kick.com":      "kick",
    "youtube.com":   "youtube",
    "youtu.be":      "youtube",
}


def _platform_from_url(url: str) -> str:
    u = url.lower()
    if "instagram.com" in u:                    return "Instagram"
    if "tiktok.com" in u:                       return "TikTok"
    if "twitter.com" in u or "x.com" in u:      return "Twitter"
    if "facebook.com" in u or "fb.com" in u:    return "Facebook"
    if "youtube.com" in u or "youtu.be" in u:   return "Youtube"
    if "twitch.tv" in u:                         return "Twitch"
    if "kick.com" in u:                          return "Kick"
    if "reddit.com" in u:                        return "Reddit"
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


def _resolve_cookies(url: str) -> str | None:
    url_lower = url.lower()
    for domain, name in _SITE_COOKIE_MAP.items():
        if domain in url_lower:
            path = Path(COOKIES_DIR) / f"{name}.txt"
            return str(path) if path.exists() else None
    return None


async def identify_post(url: str) -> dict:
    """Determine media type (video / photo / carousel) without downloading."""
    loop = asyncio.get_running_loop()
    cookiepath = _resolve_cookies(url)
    opts: dict = {"quiet": True, "no_warnings": True}
    if cookiepath:
        opts["cookiefile"] = cookiepath
    # Cloudflare-protected sites need Chrome TLS impersonation (HTTP 406 otherwise).
    from .live_downloader import _add_impersonate_if_needed
    _add_impersonate_if_needed(opts, url)

    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    try:
        info = await loop.run_in_executor(None, _run)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "No video formats found" in msg:
            return {
                "platform": _platform_from_url(url),
                "uploader": None, "uploader_id": None,
                "media_type": "photo", "count": 1,
                "is_private": False, "title": None,
            }
        is_private = any(k in msg.lower() for k in ("private", "login", "sign in", "restricted"))
        return {"error": msg[:300], "is_private": is_private}
    except Exception as e:
        return {"error": str(e)[:300], "is_private": False}

    entries = list(info.get("entries") or [])
    if entries or info.get("_type") == "playlist":
        media_type, count = "carousel", len(entries)
    else:
        vcodec = info.get("vcodec") or ""
        ext = (info.get("ext") or "").lower()
        if ext in ("jpg", "jpeg", "png", "webp") or vcodec in ("none", ""):
            media_type, count = "photo", 1
        else:
            media_type, count = "video", 1

    is_live = bool(info.get("is_live")) or (info.get("live_status") or "").lower() in ("is_live", "is_upcoming", "post_live")

    return {
        "platform":    info.get("extractor_key", "unknown"),
        "uploader":    info.get("uploader") or info.get("channel"),
        "uploader_id": info.get("uploader_id") or info.get("channel_id"),
        "media_type":  "live" if is_live else media_type,
        "count":       count,
        "is_private":  False,
        "is_live":     is_live,
        "live_status": (info.get("live_status") or "").lower() or None,
        "title":       info.get("title"),
    }


class _GalleryDLFallback(Exception):
    pass


def _gallery_dl_run(base_dir: str, url: str, cookiepath: str | None) -> list[str]:
    Path(base_dir).mkdir(parents=True, exist_ok=True)
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


async def download(
    url: str,
    media_type: str | None = None,
    is_owner: bool = True,
    quality: str | None = None,
) -> dict:
    """Download url. Returns {files: [...], cached: bool} or {error: str}.

    If quality is None, falls back to the global DEFAULT_QUALITY config.
    Per-chat preference (via /default_video_size) flows through here.
    """
    temp = not is_owner

    if not temp:
        cached = await db.get_url_cache(url)
        if cached:
            return {"files": cached["files"], "cached": True}

    async with _get_semaphore():
        loop = asyncio.get_running_loop()
        cookiepath = _resolve_cookies(url)
        out_dir = TEMP_DIR if temp else DOWNLOADS_DIR
        Path(out_dir).mkdir(parents=True, exist_ok=True)

        try:
            if media_type in ("photo", "carousel"):
                all_files = await loop.run_in_executor(
                    None, _gallery_dl_run, out_dir, url, cookiepath
                )
            else:
                effective_quality = quality or DEFAULT_QUALITY
                merge_fmt = "mp4"
                fmt_str   = _quality_to_format(effective_quality)

                if temp:
                    outtmpl = f"{out_dir}/%(title).80s.%(ext)s"
                else:
                    outtmpl = f"{out_dir}/%(extractor)s/%(uploader,uploader_id)s/%(title).80s.%(ext)s"

                final: dict = {"path": None, "prepared": None}

                def hook(d):
                    if d["status"] == "finished" and not final["path"]:
                        final["path"] = d.get("filename")

                ydl_opts = {
                    "format":               fmt_str,
                    "outtmpl":              outtmpl,
                    "merge_output_format":  merge_fmt,
                    "quiet":                True,
                    "no_warnings":          True,
                    "progress_hooks":       [hook],
                    "postprocessors":       [{"key": "FFmpegMetadata"}],
                }
                if cookiepath:
                    ydl_opts["cookiefile"] = cookiepath

                def _run():
                    try:
                        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                            info = ydl.extract_info(url, download=True)
                            final["prepared"] = ydl.prepare_filename(info)
                            if not final["path"]:
                                final["path"] = final["prepared"]
                    except yt_dlp.utils.DownloadError as e:
                        if "No video formats found" not in str(e):
                            raise
                        raise _GalleryDLFallback()

                try:
                    await loop.run_in_executor(None, _run)
                except _GalleryDLFallback:
                    all_files = await loop.run_in_executor(
                        None, _gallery_dl_run, out_dir, url, cookiepath
                    )
                    if not temp:
                        uploader = Path(all_files[0]).parent.name
                        await db.set_url_cache(url, all_files, _platform_from_url(url), uploader)
                    return {"files": all_files, "cached": False}

                raw = Path(final["path"] or "")
                clean_stem = re.sub(r"\.f\d+$", "", raw.stem)
                merged = raw.parent / f"{clean_stem}.{merge_fmt}"
                if merged.exists():
                    filepath = str(merged)
                else:
                    prepared = Path(final.get("prepared") or "")
                    filepath = str(prepared) if prepared.exists() else str(raw)

                all_files = [filepath]

            if not temp:
                uploader = Path(all_files[0]).parent.name if all_files else None
                await db.set_url_cache(url, all_files, _platform_from_url(url), uploader)

            return {"files": all_files, "cached": False}

        except Exception as exc:
            return {"error": str(exc)[:500]}


async def send_file(bot, chat_id: int, filepath: str, caption: str | None = None) -> dict:
    path = Path(filepath)
    if not path.exists():
        return {"error": f"File not found: {filepath}"}

    size = path.stat().st_size
    if size > TELEGRAM_MAX_BYTES:
        return {"error": "file_too_large", "size_mb": round(size / 1024 / 1024, 1)}

    ext = path.suffix.lower()
    size_mb = round(size / 1024 / 1024, 1)

    with open(filepath, "rb") as f:
        if ext in _IMAGE_EXTS:
            await bot.send_photo(chat_id=chat_id, photo=f, caption=caption,
                                 read_timeout=60, write_timeout=60)
        elif ext in _ANIM_EXTS:
            await bot.send_animation(chat_id=chat_id, animation=f, caption=caption,
                                     read_timeout=60, write_timeout=60)
        elif ext in _VIDEO_EXTS:
            await bot.send_video(chat_id=chat_id, video=f, caption=caption,
                                 read_timeout=120, write_timeout=120)
        elif ext in _AUDIO_EXTS:
            await bot.send_audio(chat_id=chat_id, audio=f, caption=caption,
                                 read_timeout=120, write_timeout=120)
        else:
            await bot.send_document(chat_id=chat_id, document=f, caption=caption,
                                    read_timeout=120, write_timeout=120)

    return {"ok": True, "size_mb": size_mb}


async def send_files(bot, chat_id: int, filepaths: list[str], caption: str | None = None) -> dict:
    """Send one or more files. Groups images into a media group (up to 10)."""
    filepaths = [f for f in filepaths if Path(f).exists()]
    if not filepaths:
        return {"error": "No files found to send"}
    if len(filepaths) == 1:
        return await send_file(bot, chat_id, filepaths[0], caption)

    all_images = all(Path(f).suffix.lower() in _IMAGE_EXTS for f in filepaths)
    if all_images:
        handles = []
        try:
            media = []
            for i, fp in enumerate(filepaths[:10]):
                f = open(fp, "rb")
                handles.append(f)
                media.append(InputMediaPhoto(media=f, caption=caption if i == 0 else None))
            await bot.send_media_group(chat_id=chat_id, media=media,
                                       read_timeout=120, write_timeout=120)
        finally:
            for f in handles:
                f.close()
        return {"ok": True, "count": len(media)}

    sent = 0
    for i, fp in enumerate(filepaths):
        result = await send_file(bot, chat_id, fp, caption if i == 0 else None)
        if result.get("ok"):
            sent += 1
    return {"ok": True, "count": sent}


async def cleanup_temp_files() -> None:
    """Delete files in TEMP_DIR older than TEMP_TTL_HOURS hours."""
    import time
    temp_path = Path(TEMP_DIR)
    if not temp_path.exists():
        return
    cutoff = time.time() - TEMP_TTL_HOURS * 3600
    for f in temp_path.rglob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            try:
                f.unlink()
            except Exception:
                pass
    for d in sorted(temp_path.rglob("*"), reverse=True):
        if d.is_dir():
            try:
                d.rmdir()
            except OSError:
                pass


async def start_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            await cleanup_temp_files()
        except Exception:
            pass
