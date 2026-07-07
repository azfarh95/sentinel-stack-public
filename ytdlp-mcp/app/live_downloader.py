"""Livestream recording for SMDL v2.

Design contract (per user 2026-05-10):
- ZERO retries on session/auth failures. If cookies expire or yt-dlp gets a
  401/403/private/login error mid-stream, abort cleanly with the bytes we
  already have. Don't waste hours retrying a failed login.
- Platform whitelist. TikTok and Instagram are explicitly OFF — both have
  hostile anti-bot infra and live recording fails unpredictably. YouTube /
  Twitch / Kick work reliably.
- Disk pre-check. Refuse to start if free space < LIVE_MIN_FREE_DISK_GB.
- Heartbeat every LIVE_HEARTBEAT_SECONDS, not per chunk — Telegram rate
  limits + reduced noise.
- One job per chat at a time (LIVE_MAX_CONCURRENT). Live URLs are long-
  running; queueing them implicitly via the global semaphore would block
  regular downloads.

This module is intentionally separate from `downloader.py` because live is
a different paradigm: long-lived async generator vs short-lived future.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import signal
import time
from pathlib import Path
from typing import AsyncIterator

import yt_dlp

from .config import (
    LIVE_ABORT_ON_SESSION_FAIL,
    LIVE_HEARTBEAT_SECONDS,
    LIVE_MAX_CONCURRENT,
    LIVE_MAX_HEIGHT,
    LIVE_MIN_FREE_DISK_GB,
    LIVE_PLATFORMS,
    LIVE_TRANSCODE_HEIGHT,
    LIVE_TRANSCODE_KEEP_ORIGINAL,
)

logger = logging.getLogger(__name__)

DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/downloads")
LIVE_DIR      = os.path.join(DOWNLOADS_DIR, "live")

# Patterns in yt-dlp errors that indicate session/auth failure rather than
# transient network issues. On any of these, abort instantly — retrying
# will not help.
_AUTH_FAIL_PATTERNS = re.compile(
    r"(?i)("
    r"login\s*required|"
    r"sign\s*in\s*required|"
    r"private\s*video|"
    r"members\s*only|"
    r"403\s*forbidden|"
    r"401\s*unauthorized|"
    r"cookie.*invalid|"
    r"cookies?\s+have\s+expired|"
    r"unable\s+to\s+download.*authentication|"
    r"this\s+content\s+is\s+only\s+available"
    r")"
)

# Patterns indicating yt-dlp doesn't support / can't extract from this site.
# Distinct from auth failures — retrying helps for auth, never for these.
_NO_EXTRACTOR_PATTERNS = re.compile(
    r"(?i)("
    r"unsupported\s*url|"
    r"no\s*video\s*formats?\s*found|"
    r"no\s*suitable\s*extractor|"
    r"no\s*extractor\s*matches|"
    r"requested\s*format\s*is\s*not\s*available|"
    r"no\s*streams?\s*available"
    r")"
)


def _classify_yt_dlp_error(err_text: str) -> str:
    """Map yt-dlp error text to one of: 'session_fail', 'no_extractor', 'transient', 'unknown'."""
    if _AUTH_FAIL_PATTERNS.search(err_text):
        return "session_fail"
    if _NO_EXTRACTOR_PATTERNS.search(err_text):
        return "no_extractor"
    return "transient"


_live_semaphore: asyncio.Semaphore | None = None


def _get_live_semaphore() -> asyncio.Semaphore:
    global _live_semaphore
    if _live_semaphore is None:
        _live_semaphore = asyncio.Semaphore(LIVE_MAX_CONCURRENT)
    return _live_semaphore


# Hosts behind Cloudflare bot management. yt-dlp's default TLS fingerprint
# gets HTTP 406 from these — add --impersonate=chrome via curl_cffi to bypass.
# Core ships an empty set; plugins register specific hosts via
# register_cloudflare_host(...). Keeps host-specific knowledge out of the
# public scope while letting the impersonation MECHANISM be public.
_CLOUDFLARE_HOSTS: set[str] = set()


def register_cloudflare_host(*hosts: str) -> None:
    """Plugin entry-point: declare additional hosts that need Chrome TLS
    impersonation to bypass Cloudflare bot challenges."""
    for h in hosts:
        if h:
            _CLOUDFLARE_HOSTS.add(h.lower())


def _needs_impersonate(url: str) -> bool:
    u = url.lower()
    return any(h in u for h in _CLOUDFLARE_HOSTS)


def _add_impersonate_if_needed(ydl_opts: dict, url: str) -> dict:
    """Mutate ydl_opts to add Chrome TLS impersonation for sites that need it.
    Silent no-op if curl_cffi isn't installed (logs a warning instead)."""
    if not _needs_impersonate(url):
        return ydl_opts
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        ydl_opts["impersonate"] = ImpersonateTarget("chrome")
    except ImportError:
        logger.warning(
            "Cloudflare-protected URL detected but curl_cffi not installed — "
            "yt-dlp will likely get HTTP 406. Install with: pip install curl_cffi"
        )
    return ydl_opts


# Platform-name labels: hostname-substring → friendly name. Core ships
# mainstream platforms. Plugins can append via register_platform_label.
_PLATFORM_LABELS: list[tuple[tuple[str, ...], str]] = [
    (("youtube.com", "youtu.be"),       "youtube"),
    (("twitch.tv",),                    "twitch"),
    (("kick.com",),                     "kick"),
    (("tiktok.com",),                   "tiktok"),
    (("instagram.com",),                "instagram"),
    (("facebook.com", "fb.com"),        "facebook"),
]


def register_platform_label(label: str, *host_substrings: str) -> None:
    """Plugin entry-point: register a friendly label for a hostname pattern."""
    if host_substrings:
        _PLATFORM_LABELS.append((tuple(host_substrings), label))


def _platform_of(url: str) -> str:
    """Pure classifier — returns a friendly platform name, no allow/block decision."""
    u = url.lower()
    for hosts, label in _PLATFORM_LABELS:
        if any(h in u for h in hosts):
            return label
    return "other"


def _platform_allowed(url: str) -> tuple[bool, str]:
    """Adaptive policy: trust yt-dlp's 1700+ extractors. The LIVE_PLATFORMS
    whitelist is now ADVISORY rather than a gate — used only to flag
    'known-good' platforms in user messages. Any URL is allowed; yt-dlp
    decides via extraction. Failures bubble up as LiveAbort with reason
    'no_extractor' / 'no_format' which the bot turns into the user-friendly
    'Site not supported / not configured yet' after the retry budget is spent.
    """
    return (True, _platform_of(url))


def _free_disk_gb(path: str) -> float:
    try:
        usage = shutil.disk_usage(path)
        return usage.free / (1024 ** 3)
    except Exception:
        return -1.0


def detect_live(info: dict | None) -> bool:
    """Return True if yt-dlp's extracted info dict says this is a live stream."""
    if not info:
        return False
    if info.get("is_live"):
        return True
    status = (info.get("live_status") or "").lower()
    return status in ("is_live", "is_upcoming", "post_live")


class LiveAbort(Exception):
    """Raised to signal an abort that should NOT trigger a retry."""
    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _is_auth_failure(err_text: str) -> bool:
    return bool(_AUTH_FAIL_PATTERNS.search(err_text))


def _is_no_extractor(err_text: str) -> bool:
    return bool(_NO_EXTRACTOR_PATTERNS.search(err_text))


def _list_ffmpeg_children() -> list[int]:
    """Enumerate ffmpeg/ffprobe PIDs that are children of THIS process."""
    if not os.path.isdir("/proc"):
        return []
    my_pid = os.getpid()
    pids = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/status") as f:
                    status = f.read()
                if f"PPid:\t{my_pid}\n" not in status:
                    continue
                with open(f"/proc/{entry}/comm") as f:
                    comm = f.read().strip()
                if comm in ("ffmpeg", "ffprobe"):
                    pids.append(int(entry))
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                continue
    except Exception as e:
        logger.warning("ffmpeg-list failed: %s", e)
    return pids


def _kill_orphan_ffmpeg_children(use_kill: bool = False) -> int:
    """SIGTERM (or SIGKILL if use_kill=True) any ffmpeg/ffprobe child."""
    sig = signal.SIGKILL if use_kill else signal.SIGTERM
    sig_name = "SIGKILL" if use_kill else "SIGTERM"
    killed = 0
    for pid in _list_ffmpeg_children():
        try:
            os.kill(pid, sig)
            killed += 1
            logger.info("sent %s to ffmpeg-family PID %d", sig_name, pid)
        except ProcessLookupError:
            pass  # already gone
    return killed


def _finalize_partial_recording(path: str) -> str:
    """Remux a possibly-broken livestream file so its duration metadata
    matches the actual playable bytes.

    Why this matters: livestream recordings interrupted mid-stream leave
    .part files (or .ts/.mkv) whose moov atom / container index claims a
    duration based on what the LIVE stream's elapsed-time was, not what
    the recorded bytes actually contain. Android's gallery — and many
    other players — read the metadata and trust it, so a 2-min recording
    of a 3-hour stream displays as 3 hours of black frames.

    Fix: ffmpeg remux with -c copy. Lossless (no re-encode), recomputes
    duration from actual packet PTS, and writes a proper moov atom at
    the start (+faststart) so streaming/seeking works.

    Returns the remuxed path on success; returns the original path on
    failure (a file with bad metadata is still better than no file).
    """
    src = Path(path)
    if not src.exists() or src.stat().st_size == 0:
        return path

    # If it's already a clean .mp4 (no .part suffix) AND ffprobe agrees the
    # duration is sane, skip remux. Cheap heuristic: just check extension.
    # We always remux .part / .ts / .mkv since those are likely interrupted.
    name = src.name.lower()
    is_partial = (
        name.endswith(".part")
        or name.endswith(".mp4.part")
        or name.endswith(".mkv.part")
        or name.endswith(".ts")
    )
    if not is_partial:
        return path

    # Output: strip .part if present, otherwise change ext to .mp4
    if name.endswith(".part"):
        out_name = src.name[:-len(".part")]
    else:
        out_name = src.stem + ".mp4"
    if not out_name.lower().endswith(".mp4"):
        out_name += ".mp4"
    out = src.with_name(out_name)

    # Avoid clobbering an existing finalized file that yt-dlp already merged
    # (rare but possible if both .part and final .mp4 are present).
    if out.exists() and out != src and out.stat().st_size > 0:
        logger.info("finalize: %s already exists, skipping remux of %s", out.name, src.name)
        return str(out)

    # Use a temp output then rename atomically — avoids leaving a half-written
    # file at the target name if ffmpeg crashes.
    tmp_out = src.with_name(out.name + ".finalize.tmp.mp4")

    import subprocess
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-i", str(src),
        "-c", "copy",
        "-movflags", "+faststart",
        str(tmp_out),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        logger.error("finalize: ffmpeg remux timed out for %s", src.name)
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        return path
    except Exception as e:
        logger.error("finalize: ffmpeg remux failed to spawn: %s", e)
        return path

    if proc.returncode != 0 or not tmp_out.exists() or tmp_out.stat().st_size == 0:
        logger.error(
            "finalize: ffmpeg remux failed (rc=%s) — keeping original. stderr: %s",
            proc.returncode, (proc.stderr or "")[:400],
        )
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass
        return path

    # Success. Move tmp into place, delete original .part.
    try:
        tmp_out.replace(out)
        if src != out:
            src.unlink(missing_ok=True)
        logger.info(
            "finalize: remuxed %s → %s (%.1f MB)",
            src.name, out.name, out.stat().st_size / 1024 / 1024,
        )
        return str(out)
    except Exception as e:
        logger.error("finalize: rename/cleanup failed: %s", e)
        # tmp_out exists but wasn't moved — keep it as the result if we can
        if tmp_out.exists():
            return str(tmp_out)
        return path


async def _periodic_progress_reporter(state: dict, on_progress, interval_sec: int = 60):
    """Timer-based progress emitter — runs every interval_sec.

    Why this exists: yt-dlp's progress hook only fires when yt-dlp is the
    downloader. For Twitch live HLS, yt-dlp delegates to ffmpeg, and the
    hook never fires during the actual recording — only at start/finish.
    Without this timer, the user sees a stale 'Recording 1 min · 43 MB'
    forever until the recording stops.

    This task reads the current .part file size from disk so progress
    updates work regardless of which downloader yt-dlp picked.
    """
    while not state.get("done"):
        try:
            await asyncio.sleep(interval_sec)
        except asyncio.CancelledError:
            return
        if state.get("done"):
            return
        # If we don't yet know the output filepath, hunt for the newest
        # .part file in LIVE_DIR (created since this recording started).
        filepath = state.get("filepath")
        if not filepath:
            try:
                started = state.get("started_at", 0)
                candidates = [
                    p for p in Path(LIVE_DIR).rglob("*.part")
                    if p.stat().st_mtime >= started
                ]
                if candidates:
                    newest = max(candidates, key=lambda p: p.stat().st_mtime)
                    filepath = str(newest)
                    state["filepath"] = filepath
            except Exception:
                pass
        # Read on-disk size for the .part (or completed .mp4) file
        if filepath:
            try:
                state["bytes"] = Path(filepath).stat().st_size
            except (OSError, FileNotFoundError):
                pass
        elapsed = int(time.time() - state["started_at"])
        try:
            await on_progress({
                "status":          "recording",
                "elapsed_seconds": elapsed,
                "bytes":           state["bytes"],
                "detail":          "live",
            })
        except Exception:
            pass  # rate-limited / message-edit-not-modified / etc.


def _transcode_to_height(path: str, target_h: int, keep_original: bool) -> str:
    """Re-encode the recorded file to a lower resolution.

    Two modes:
      keep_original=True   → produce <name>.{H}p.mp4 sibling, return its path
      keep_original=False  → replace the input file (smaller archive)

    Returns the path to deliver. On any failure, returns the original path
    (a working file is better than a broken transcode).

    CPU cost: libx264 veryfast crf 23 → roughly 1-2x realtime per video.
    A 1-hour 720p source transcodes to 480p in ~30-60 min on a typical CPU.
    """
    src = Path(path)
    if not src.exists() or src.stat().st_size == 0 or target_h <= 0:
        return path

    # Skip if source is already at-or-below target (avoid pointless re-encode)
    try:
        import subprocess as _sp
        probe = _sp.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0", str(src)],
            capture_output=True, text=True, timeout=30,
        )
        cur_h = int((probe.stdout or "0").strip() or 0)
        if 0 < cur_h <= target_h:
            logger.info("transcode: source is %dp (≤ target %dp), skipping", cur_h, target_h)
            return path
    except Exception:
        pass  # if probe fails, just attempt transcode

    # Output naming: foo.mp4 → foo.480p.mp4 (or replace if not keeping original)
    out = src.with_name(f"{src.stem}.{target_h}p.mp4")
    if not keep_original:
        # Replace mode: write to a temp sibling, then rename over the original
        out = src.with_name(f"{src.stem}.transcode.tmp.mp4")

    import subprocess
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
        "-i", str(src),
        "-vf", f"scale=-2:{target_h}",  # -2 keeps width even, auto-derived
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(out),
    ]
    logger.info("transcode: %s → %dp (keep_original=%s)", src.name, target_h, keep_original)
    try:
        # No timeout — long streams can take real time. Caller is async-aware.
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except Exception as e:
        logger.error("transcode: ffmpeg spawn failed: %s", e)
        return path

    if proc.returncode != 0 or not out.exists() or out.stat().st_size == 0:
        logger.error(
            "transcode: ffmpeg failed (rc=%s) — keeping original. stderr: %s",
            proc.returncode, (proc.stderr or "")[:400],
        )
        try:
            out.unlink(missing_ok=True)
        except Exception:
            pass
        return path

    # Success path
    if keep_original:
        logger.info(
            "transcode: produced %s (%.1f MB) alongside original (%.1f MB)",
            out.name, out.stat().st_size / 1024 / 1024,
            src.stat().st_size / 1024 / 1024,
        )
        return str(out)
    else:
        # Replace mode: rename tmp into the original's path, delete original
        final = src  # replace at the original path
        try:
            out.replace(final)
            logger.info(
                "transcode: replaced %s with %dp version (%.1f MB)",
                src.name, target_h, final.stat().st_size / 1024 / 1024,
            )
            return str(final)
        except Exception as e:
            logger.error("transcode: replace rename failed: %s", e)
            if out.exists():
                return str(out)
            return path


async def _stop_deadline_watchdog(stop_flag: dict, deadline_sec: int = 15):
    """Bridge-style watchdog: when stop_flag is set, give graceful path
    deadline_sec to terminate. If ffmpeg still alive after that, SIGTERM
    the subprocess tree. After another deadline_sec, SIGKILL anything left.

    Solves the orphan-ffmpeg case where yt-dlp sees the LiveAbort exception
    in its progress hook but ffmpeg-as-external-downloader is blocking on
    I/O and doesn't respond. Without this watchdog, yt-dlp's `ydl.download()`
    waits indefinitely on ffmpeg, the LiveAbort never propagates up, and
    bot.py never receives the result.
    """
    while True:
        await asyncio.sleep(2)
        if not stop_flag.get("stop"):
            continue
        # stop_flag is set — start the deadline timer
        await asyncio.sleep(deadline_sec)
        ffmpeg_pids = _list_ffmpeg_children()
        if ffmpeg_pids:
            logger.warning(
                "stop_flag set %ds ago but ffmpeg children still alive: %s — sending SIGTERM",
                deadline_sec, ffmpeg_pids,
            )
            _kill_orphan_ffmpeg_children(use_kill=False)
            await asyncio.sleep(deadline_sec)
            ffmpeg_pids = _list_ffmpeg_children()
            if ffmpeg_pids:
                logger.error(
                    "ffmpeg ignored SIGTERM after another %ds — escalating to SIGKILL: %s",
                    deadline_sec, ffmpeg_pids,
                )
                _kill_orphan_ffmpeg_children(use_kill=True)
        return  # watchdog done — either ffmpeg gone, or we killed it


async def record_live(
    url: str,
    cookiepath: str | None,
    on_progress: callable | None = None,
    stop_flag: dict | None = None,
    transcode_height: int | None = None,
    transcode_keep_original: bool | None = None,
) -> dict:
    """Record an active livestream.

    Returns:
        {
            "ok": bool,
            "files": [filepath],
            "duration_seconds": int,
            "bytes_downloaded": int,
            "abort_reason": str | None,  # one of: 'session_fail', 'disk_full',
                                          #         'stream_ended', 'unknown'
        }

    on_progress is an optional async callback receiving:
        {"status": "recording" | "ended" | "aborted",
         "elapsed_seconds": int,
         "bytes": int,
         "detail": str}
    Called at most every LIVE_HEARTBEAT_SECONDS.
    """
    allowed, platform = _platform_allowed(url)
    if not allowed:
        return {
            "ok": False,
            "files": [],
            "duration_seconds": 0,
            "bytes_downloaded": 0,
            "abort_reason": "platform_not_allowed",
            "detail": f"Live recording disabled for {platform}. Whitelist: {sorted(LIVE_PLATFORMS)}.",
        }

    free_gb = _free_disk_gb(DOWNLOADS_DIR)
    if 0 < free_gb < LIVE_MIN_FREE_DISK_GB:
        return {
            "ok": False,
            "files": [],
            "duration_seconds": 0,
            "bytes_downloaded": 0,
            "abort_reason": "disk_low",
            "detail": f"Only {free_gb:.1f} GB free; need ≥ {LIVE_MIN_FREE_DISK_GB} GB.",
        }

    Path(LIVE_DIR).mkdir(parents=True, exist_ok=True)

    # Capture the *running* event loop on the calling (asyncio) thread BEFORE
    # we hand off to run_in_executor. The hook runs in the executor's worker
    # thread where asyncio.get_event_loop() returns a fresh, non-running loop
    # on Python 3.12 — calling run_coroutine_threadsafe on that is a silent
    # no-op. Capturing here and closing over it fixes that.
    main_loop = asyncio.get_running_loop()

    state = {
        "started_at": time.time(),
        "last_heartbeat": 0.0,
        "bytes": 0,
        "filepath": None,
        "abort_reason": None,
        "abort_detail": "",
    }

    def _maybe_emit(now: float):
        if on_progress is None:
            return
        if now - state["last_heartbeat"] < LIVE_HEARTBEAT_SECONDS:
            return
        state["last_heartbeat"] = now
        elapsed = int(now - state["started_at"])
        try:
            asyncio.run_coroutine_threadsafe(
                on_progress({
                    "status":          "recording",
                    "elapsed_seconds": elapsed,
                    "bytes":           state["bytes"],
                    "detail":          "live",
                }),
                main_loop,
            )
        except RuntimeError:
            # Loop not running — fire-and-forget.
            pass

    def hook(d):
        # User-requested stop wins over everything else.
        if stop_flag is not None and stop_flag.get("stop"):
            state["abort_reason"] = "user_stopped"
            raise LiveAbort("user_stopped", "stop requested by user")
        # Throttled heartbeat. yt-dlp calls this many times per second on
        # active live streams; we only emit every LIVE_HEARTBEAT_SECONDS.
        now = time.time()
        if d.get("status") == "downloading":
            state["bytes"] = d.get("downloaded_bytes") or state["bytes"]
            _maybe_emit(now)
        elif d.get("status") == "finished":
            state["filepath"] = d.get("filename")
        elif d.get("status") == "error":
            err = str(d.get("info_dict", {}).get("error") or d)
            if LIVE_ABORT_ON_SESSION_FAIL and _is_auth_failure(err):
                state["abort_reason"] = "session_fail"
                state["abort_detail"] = err[:300]
                raise LiveAbort("session_fail", err[:300])

    # Bridge yt-dlp's own logger into ours so we can SEE what it's doing.
    # With quiet: True (the previous default) yt-dlp errors got swallowed,
    # leaving 0-byte .part files with no diagnostic trail.
    class _YtdlpLogger:
        def debug(self, msg):
            if msg.startswith("[debug]"):
                logger.debug("yt-dlp: %s", msg)
            else:
                logger.info("yt-dlp: %s", msg)
        def info(self, msg):    logger.info("yt-dlp: %s", msg)
        def warning(self, msg): logger.warning("yt-dlp: %s", msg)
        def error(self, msg):   logger.error("yt-dlp: %s", msg)

    # Build format selector from live_max_height. 0 = no cap.
    if LIVE_MAX_HEIGHT > 0:
        format_selector = f"bestvideo[height<={LIVE_MAX_HEIGHT}]+bestaudio/best[height<={LIVE_MAX_HEIGHT}]/best"
    else:
        format_selector = "bestvideo+bestaudio/best"

    ydl_opts = {
        "format":               format_selector,
        "outtmpl":              f"{LIVE_DIR}/%(extractor)s/%(uploader,uploader_id)s/%(title).80s.%(timestamp)s.%(ext)s",
        "merge_output_format":  "mp4",
        "logger":               _YtdlpLogger(),
        "quiet":                False,
        "no_warnings":          False,
        "progress_hooks":       [hook],
        "wait_for_video":       (1, 30),  # if 'is_upcoming', poll up to 30s — anything longer, give up
        # CRITICAL: zero retries. Auth/session failures should NOT loop.
        "retries":              0,
        "fragment_retries":     0,
        "extractor_retries":    0,
        "file_access_retries":  0,
        "skip_unavailable_fragments": False,
        "abort_on_unavailable_fragment": True,
        # Force native Python HLS downloader instead of ffmpeg subprocess.
        # Reason: when the user calls /stop_livestream, the progress hook
        # raises LiveAbort INSIDE Python — but if yt-dlp had delegated to
        # ffmpeg, ffmpeg keeps writing fragments forever as an orphan child.
        # Native downloader runs in-process so the exception interrupts it.
        "hls_prefer_native":    True,
        "external_downloader":  {"m3u8": "native", "default": "native"},
    }
    # `live_from_start` is YouTube-only. Twitch / Kick raise
    # "no formats that can be downloaded from the start" if it's set —
    # those platforms only support recording from "now" forward. Only
    # enable for YouTube, where it materially improves "joined late"
    # recordings.
    if platform == "youtube":
        ydl_opts["live_from_start"] = True
    if cookiepath:
        ydl_opts["cookiefile"] = cookiepath
    _add_impersonate_if_needed(ydl_opts, url)

    async with _get_live_semaphore():
        loop = asyncio.get_running_loop()

        # Bridge-style watchdog: enforces stop_flag. If yt-dlp/ffmpeg ignore
        # the graceful-exit path (Python progress-hook can't interrupt ffmpeg's
        # blocking I/O), this kills the subprocess tree after a deadline.
        watchdog_task = None
        if stop_flag is not None:
            watchdog_task = asyncio.create_task(
                _stop_deadline_watchdog(stop_flag, deadline_sec=15)
            )

        # Timer-based progress reporter — guarantees periodic UI updates
        # even when ffmpeg is the downloader and yt-dlp's hook isn't firing.
        progress_task = None
        if on_progress is not None:
            progress_task = asyncio.create_task(
                _periodic_progress_reporter(state, on_progress, interval_sec=60)
            )

        def _run():
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
            except LiveAbort:
                raise
            except yt_dlp.utils.DownloadError as e:
                msg = str(e)
                # User-initiated stop wins over any DownloadError. yt-dlp's
                # progress hook can't intercept ffmpeg-as-external-downloader,
                # so when the watchdog SIGTERMs ffmpeg on /stop_livestream,
                # yt-dlp surfaces the kill as "ffmpeg exited with code 255".
                # That isn't a real error — it's the user-stop landing.
                if stop_flag is not None and stop_flag.get("stop"):
                    raise LiveAbort("user_stopped", "stop requested by user")
                if "mouflon" in msg.lower():
                    raise LiveAbort("mouflon_blocked", msg[:300])
                if _is_auth_failure(msg):
                    raise LiveAbort("session_fail", msg[:300])
                if _is_no_extractor(msg):
                    raise LiveAbort("no_extractor", msg[:300])
                # Stream-ended is the canonical "this is fine" terminal state
                if any(k in msg.lower() for k in ("ended", "no longer live", "stream is offline")):
                    return  # natural end
                raise LiveAbort("download_error", msg[:300])
            except Exception as e:
                # Same logic — defensive against any other exception type
                # raised after a user-initiated stop.
                if stop_flag is not None and stop_flag.get("stop"):
                    raise LiveAbort("user_stopped", "stop requested by user")
                raise LiveAbort("unknown", str(e)[:300])

        try:
            await loop.run_in_executor(None, _run)
            abort_reason = state.get("abort_reason") or "stream_ended"
        except LiveAbort as e:
            abort_reason = e.reason
            state["abort_detail"] = e.detail
        except Exception as e:
            logger.exception("Unexpected live recording failure")
            abort_reason = "unknown"
            state["abort_detail"] = str(e)[:300]
        finally:
            # Mark recording done so the progress reporter exits its loop
            state["done"] = True
            # Cancel the watchdog if recording finished naturally before deadline
            if watchdog_task and not watchdog_task.done():
                watchdog_task.cancel()
                try:
                    await watchdog_task
                except asyncio.CancelledError:
                    pass
            # Cancel the progress reporter
            if progress_task and not progress_task.done():
                progress_task.cancel()
                try:
                    await progress_task
                except asyncio.CancelledError:
                    pass
            # Defensive: kill any ffmpeg/ffprobe children. With native HLS
            # downloader this should be a no-op, but if a code path slips
            # through to ffmpeg we don't leak orphan recorders.
            killed = _kill_orphan_ffmpeg_children()
            if killed:
                logger.info("orphan cleanup terminated %d ffmpeg child(ren)", killed)

    elapsed = int(time.time() - state["started_at"])
    files = []
    if state["filepath"] and Path(state["filepath"]).exists():
        files = [state["filepath"]]
    else:
        # Recording may have been left as a .part file (yt-dlp didn't finalize)
        # — include those in the fallback search so we don't lose multi-GB
        # recordings to "files: []" and silently skip the delivery message.
        try:
            patterns = ("*.mp4", "*.mp4.part", "*.mkv", "*.mkv.part", "*.ts")
            for pat in patterns:
                for f in Path(LIVE_DIR).rglob(pat):
                    if f.stat().st_mtime >= state["started_at"] and str(f) not in files:
                        files.append(str(f))
        except Exception:
            pass

    # Finalize: any .part / .ts / .mkv file gets ffmpeg-remuxed so its
    # container duration matches the actual playable bytes. Without this,
    # interrupted recordings show 'wall-clock-of-stream' duration in
    # players (Android gallery: 3 hours of black frames for a 2-min clip).
    finalized: list[str] = []
    for f in files:
        try:
            new_path = _finalize_partial_recording(f)
            if new_path not in finalized:
                finalized.append(new_path)
        except Exception as e:
            logger.warning("finalize: skipping %s due to error: %s", f, e)
            if f not in finalized:
                finalized.append(f)
    files = finalized

    # Optional post-finalize transcode. Per-chat override (via kwargs) wins
    # over the global config defaults — lets a /transcode telegram command
    # set this without restarting the container.
    effective_h    = LIVE_TRANSCODE_HEIGHT       if transcode_height is None        else int(transcode_height)
    effective_keep = LIVE_TRANSCODE_KEEP_ORIGINAL if transcode_keep_original is None else bool(transcode_keep_original)

    if effective_h > 0 and abort_reason in ("stream_ended", "user_stopped"):
        loop = asyncio.get_running_loop()
        transcoded: list[str] = []
        for f in files:
            try:
                # Run in executor — ffmpeg can take real time on long recordings
                new_path = await loop.run_in_executor(
                    None,
                    _transcode_to_height, f, effective_h, effective_keep,
                )
                # If keep_original=True, deliver the smaller transcoded file but
                # leave the original on disk (caller chooses what to send).
                if effective_keep and new_path != f:
                    transcoded.append(new_path)  # smaller for delivery
                    transcoded.append(f)         # original for archive
                else:
                    transcoded.append(new_path)
            except Exception as e:
                logger.warning("transcode: skipping %s due to error: %s", f, e)
                transcoded.append(f)
        files = transcoded

    bytes_total = state["bytes"]
    if not bytes_total and files:
        try:
            bytes_total = sum(Path(f).stat().st_size for f in files)
        except Exception:
            pass

    # Defensive Mouflon heuristic: if a Stripchat-family "natural end" recording
    # came back in <60 sec with <10 MB, it's almost certainly the Mouflon
    # advert (a 24-second VOD ad served instead of the real stream). Reclassify
    # so the bot reports honestly and doesn't deliver an ad as the file.
    if (
        abort_reason == "stream_ended"
        and elapsed < 60
        and bytes_total < 10 * 1024 * 1024
        and any(h in url.lower() for h in ("stripchat.com", "xhamsterlive.com"))
    ):
        abort_reason = "mouflon_blocked"
        state["abort_detail"] = (
            "Stripchat Mouflon anti-recording intercepted the stream. "
            "Only a short ad/promo was captured."
        )

    return {
        "ok":               abort_reason == "stream_ended",
        "files":            files,
        "duration_seconds": elapsed,
        "bytes_downloaded": bytes_total,
        "abort_reason":     abort_reason,
        "detail":           state["abort_detail"],
        "platform":         platform,
    }
