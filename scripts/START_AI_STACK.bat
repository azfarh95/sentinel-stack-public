@echo off
REM ════════════════════════════════════════════════════════════════════
REM  DEPRECATED 2026-06-17 — superseded by the Sentinel Watchdog.
REM
REM  Cold-boot is now OWNED by the watchdog, not this .bat:
REM    * the "sentinel-watchdog-v2" logon task runs
REM      sentinel-watchdog\scripts\boot.bat at startup, which ensures Docker
REM      Desktop, launches the watchdog daemon, and the daemon brings the
REM      WHOLE stack up in dependency order WITH the tailscale serve-dance
REM      (a plain `compose up -d`, like this .bat did, wedges every served
REM      port on Docker-Desktop-on-Windows).
REM    * to bring the stack up on demand: click "Start stack" in the watchdog
REM      dashboard / Mini App, or POST /api/v2/stack/start.
REM
REM  This stub forwards to boot.bat so old muscle-memory still works. The
REM  original full-orchestration script is in git history (this path, before
REM  the 2026-06-17 deprecation commit).
REM ════════════════════════════════════════════════════════════════════
echo.
echo  START_AI_STACK.bat is DEPRECATED - the watchdog now owns cold-boot.
echo  Forwarding to the watchdog bootstrap (sentinel-watchdog\scripts\boot.bat)...
echo.
call "C:\Users\azfar\sentinel-watchdog\scripts\boot.bat"
