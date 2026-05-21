"""Eager filesystem watcher — auto-normalize every doc dropped into _INBOX or any
configured watch folder. Sits as Step 0 of the pipeline; downstream classifier +
parsers read the canonical OCR cache without ever invoking tesseract directly.

Design:
  • Uses `watchdog` Observer for cross-platform recursive file watching.
  • On any `created` / `modified` event for a supported extension, schedules a
    debounced call to `ocr_normalize.normalize()`. Debounce prevents thrashing
    on large copy operations.
  • Idempotent: ocr_normalize checks hash + mtime; re-events on same file are
    no-ops after cache is warm.
  • Crash-safe: a brief startup scan reconciles any files that were dropped
    while the watcher was offline.

Run:
    docker exec -d portfolio-mcp python -m app.ocr_watcher \\
        --watch "/onedrive/Sentinel Finance/_INBOX" \\
        --watch "/onedrive/Sentinel Finance/01_Bank statements" \\
        --watch "/onedrive/Sentinel Finance/02_Credit card statements"

Stop:
    docker exec portfolio-mcp pkill -f ocr_watcher
"""
from __future__ import annotations
import argparse
import logging
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from app.ocr_normalize import SUPPORTED, bulk_normalize, normalize

logging.basicConfig(level=logging.INFO,
                     format="%(asctime)s [ocr_watcher] %(message)s",
                     datefmt="%H:%M:%S")
log = logging.getLogger("ocr_watcher")

DEBOUNCE_SECONDS = 2.5   # wait this long after last event before processing


class DebouncedHandler(FileSystemEventHandler):
    """Per-path debouncer — coalesce rapid-fire created/modified events.
    Useful because OneDrive sync writes a file in chunks then renames.
    """
    def __init__(self):
        self._pending: dict[Path, threading.Timer] = {}
        self._lock = threading.Lock()
        self.stats = defaultdict(int)

    def _process(self, path: Path):
        with self._lock:
            self._pending.pop(path, None)
        if not path.exists() or path.suffix.lower() not in SUPPORTED:
            return
        try:
            result = normalize(path)
            method = result.get("extraction_method")
            wc = result.get("word_count", 0)
            mc = result.get("min_confidence", 0)
            log.info(f"✓ {path.name[:60]:<60}  method={method:<11}  words={wc:>5}  min_conf={mc:.2f}")
            self.stats["ok"] += 1
        except Exception as e:
            log.error(f"✗ {path.name}: {str(e)[:120]}")
            self.stats["err"] += 1

    def _schedule(self, path: Path):
        if path.suffix.lower() not in SUPPORTED:
            return
        with self._lock:
            existing = self._pending.get(path)
            if existing:
                existing.cancel()
            timer = threading.Timer(DEBOUNCE_SECONDS, self._process, args=[path])
            timer.daemon = True
            self._pending[path] = timer
            timer.start()

    def on_created(self, event):
        if event.is_directory: return
        self._schedule(Path(event.src_path))

    def on_modified(self, event):
        if event.is_directory: return
        self._schedule(Path(event.src_path))

    def on_moved(self, event):
        if event.is_directory: return
        self._schedule(Path(event.dest_path))


def startup_reconcile(folders: list[Path]) -> dict:
    """Bulk-normalize on startup to catch any files dropped while the watcher
    was offline. Idempotent (mtime-cached)."""
    log.info("Startup reconciliation — walking %d folder(s)…", len(folders))
    total = {"total": 0, "extracted": 0, "cached": 0, "failed": 0}
    for f in folders:
        if not f.exists():
            log.warning("  watch folder does not exist: %s", f)
            continue
        log.info("  scanning %s", f)
        s = bulk_normalize(f, recursive=True, force=False)
        for k in total: total[k] += s[k]
    log.info("Startup done: %d total, %d freshly extracted, %d cached, %d failed",
              total["total"], total["extracted"], total["cached"], total["failed"])
    return total


def run(folders: list[Path]) -> None:
    handler = DebouncedHandler()
    observer = Observer()
    for f in folders:
        f = f.resolve()
        if not f.exists():
            log.warning("Skipping non-existent watch path: %s", f)
            continue
        observer.schedule(handler, str(f), recursive=True)
        log.info("Watching: %s (recursive)", f)
    observer.start()
    log.info("Watcher armed. Drop files into watched folders → auto-OCR.")
    try:
        while True:
            time.sleep(60)
            log.info("Heartbeat: %d ok, %d err so far",
                      handler.stats["ok"], handler.stats["err"])
    except KeyboardInterrupt:
        log.info("Stopping observer…")
        observer.stop()
    observer.join()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="append", required=True,
                    help="Folder(s) to watch recursively. Repeat for multiple.")
    ap.add_argument("--no-startup-scan", action="store_true",
                    help="Skip the startup bulk-reconcile pass.")
    args = ap.parse_args()
    folders = [Path(p) for p in args.watch]
    if not args.no_startup_scan:
        startup_reconcile(folders)
    run(folders)


if __name__ == "__main__":
    main()
