"""RecorderBridge — single source of truth for active live recordings.

Why a bridge exists at all: see workspace/proposals/2026-05-10-RecorderBridge-scope.md
TL;DR — yt-dlp+ffmpeg lifecycle ownership needs to live OUTSIDE the
record_live() call so that:
  1. Multiple call sites (Telegram bot today, MCP tool later) share one
     consistent stop/status surface.
  2. The bot loses the _active_live_jobs dict and the stop_flag plumbing —
     bridge owns that state.
  3. Future watchdog work at bridge scope can supervise N jobs at once
     instead of being co-located inside each record_live() invocation.

V1 scope (this module, today): Phases 1+3 of the scope doc. Bridge wraps
record_live, owns job state, exposes start/stop/status/list_active.
The watchdog stays inside record_live for now (already shipped), bridge
just delegates. Concurrency is owner-managed via record_live's existing
_live_semaphore (LIVE_MAX_CONCURRENT) — bridge enforces one-job-per-chat
on top of that.

V2 ideas (deferred):
- Move watchdog to bridge scope (covers stuck recordings whose progress
  hook never fires)
- Persist job state to SQLite so container restarts can finalize cleanly
- Accept session_id instead of chat_id (MCP compatibility — chat_id is
  Telegram-specific)
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from .live_downloader import record_live

logger = logging.getLogger(__name__)


# ── Public data shapes ──────────────────────────────────────────────────────
@dataclass
class JobHandle:
    """A live-recording job's identity and final state. Mutated by the
    bridge over the job's lifetime — caller should treat as read-only.
    """
    job_id:            str
    chat_id:           int
    url:               str
    started_at:        float
    platform:          Optional[str] = None
    uploader:          Optional[str] = None
    stop_requested_at: Optional[float] = None
    abort_reason:      Optional[str] = None
    bytes_downloaded:  int = 0
    filepath:          Optional[str] = None


@dataclass
class JobStatus:
    """Snapshot of a job at the moment the caller asked."""
    alive:           bool
    elapsed_seconds: int
    bytes:           int
    filepath:        Optional[str]
    abort_reason:    Optional[str]
    platform:        Optional[str] = None
    uploader:        Optional[str] = None


# ── Bridge ──────────────────────────────────────────────────────────────────
ProgressCallback = Callable[[dict], Awaitable[None]]


class RecorderBridge:
    """Owns live-recording job state. One instance per process.

    Today the bridge keys jobs by chat_id (Telegram-native). When MCP support
    lands, an alternate adapter can call .record(session_id=...) instead.
    For V1, chat_id IS the session_id.
    """

    def __init__(self) -> None:
        self._jobs:       dict[int, JobHandle]    = {}
        self._stop_flags: dict[int, dict[str, Any]] = {}
        self._tasks:      dict[int, asyncio.Task[dict]] = {}

    # ── lookups ─────────────────────────────────────────────────────────────
    def status(self, chat_id: int) -> Optional[JobStatus]:
        """Snapshot. Returns None if no job for this chat."""
        job = self._jobs.get(chat_id)
        if not job:
            return None
        return JobStatus(
            alive=True,
            elapsed_seconds=int(time.time() - job.started_at),
            bytes=job.bytes_downloaded,
            filepath=job.filepath,
            abort_reason=job.abort_reason,
            platform=job.platform,
            uploader=job.uploader,
        )

    def list_active(self) -> list[JobHandle]:
        """All currently-running jobs."""
        return list(self._jobs.values())

    def has_job(self, chat_id: int) -> bool:
        return chat_id in self._jobs

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def record(
        self,
        chat_id:      int,
        url:          str,
        *,
        cookiepath:               Optional[str] = None,
        on_progress:              Optional[ProgressCallback] = None,
        transcode_height:         Optional[int] = None,
        transcode_keep_original:  Optional[bool] = None,
        platform:                 Optional[str] = None,
        uploader:                 Optional[str] = None,
    ) -> dict:
        """Run a live recording end-to-end. Blocks until the recording
        finishes (naturally, by user-stop, or by failure).

        Concurrent calls for the same chat_id are rejected — the second
        call returns immediately with abort_reason='already_recording'
        rather than queueing or interleaving. Different chat_ids may
        record concurrently up to live_downloader's LIVE_MAX_CONCURRENT
        semaphore.

        Returns the same dict shape that record_live() returns, so the
        caller (bot.py) needs no changes to its result-handling code.
        """
        if chat_id in self._jobs:
            existing = self._jobs[chat_id]
            logger.info("bridge: rejected duplicate start for chat=%s (already on %s)",
                        chat_id, existing.url)
            return {
                "ok":               False,
                "files":            [],
                "duration_seconds": 0,
                "bytes_downloaded": 0,
                "abort_reason":     "already_recording",
                "detail":           f"Another recording is already active for this chat: {existing.url}",
                "platform":         platform,
            }

        job_id = f"{chat_id}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        handle = JobHandle(
            job_id=job_id,
            chat_id=chat_id,
            url=url,
            started_at=time.time(),
            platform=platform,
            uploader=uploader,
        )
        stop_flag: dict[str, Any] = {"stop": False}
        self._jobs[chat_id]       = handle
        self._stop_flags[chat_id] = stop_flag

        # Wrap on_progress to capture bytes/filepath updates into the handle
        async def _progress_capture(p: dict) -> None:
            handle.bytes_downloaded = p.get("bytes", handle.bytes_downloaded)
            if on_progress is not None:
                try:
                    await on_progress(p)
                except Exception:
                    pass  # caller shouldn't be able to crash the recording

        logger.info("bridge: start chat=%s url=%s job=%s", chat_id, url[:80], job_id)
        try:
            result = await record_live(
                url, cookiepath,
                on_progress=_progress_capture,
                stop_flag=stop_flag,
                transcode_height=transcode_height,
                transcode_keep_original=transcode_keep_original,
            )
            # Capture final state on the handle for later status() / archival
            handle.abort_reason     = result.get("abort_reason")
            handle.bytes_downloaded = result.get("bytes_downloaded") or handle.bytes_downloaded
            files = result.get("files") or []
            handle.filepath = files[0] if files else None
            logger.info(
                "bridge: end chat=%s job=%s reason=%s duration=%ss bytes=%s files=%d",
                chat_id, job_id, handle.abort_reason,
                result.get("duration_seconds"), handle.bytes_downloaded, len(files),
            )
            return result
        finally:
            # Always release per-chat slot — even on exception. The semaphore
            # inside record_live releases itself.
            self._jobs.pop(chat_id, None)
            self._stop_flags.pop(chat_id, None)
            self._tasks.pop(chat_id, None)

    async def stop(self, chat_id: int) -> Optional[JobStatus]:
        """Request a graceful stop. Returns the job's status snapshot at
        the moment the stop_flag was set (the job may still be finalizing).

        Returns None if no job exists for this chat.
        """
        job = self._jobs.get(chat_id)
        if not job:
            return None
        flag = self._stop_flags.get(chat_id)
        if flag is not None:
            flag["stop"] = True
            job.stop_requested_at = time.time()
            logger.info("bridge: stop requested chat=%s job=%s", chat_id, job.job_id)
        return JobStatus(
            alive=True,  # still finishing — caller doesn't await completion here
            elapsed_seconds=int(time.time() - job.started_at),
            bytes=job.bytes_downloaded,
            filepath=job.filepath,
            abort_reason=job.abort_reason,
            platform=job.platform,
            uploader=job.uploader,
        )


# Module-level singleton — one bridge per smdl process. bot.py + future MCP
# adapter both import this same instance so they see the same job set.
bridge = RecorderBridge()
