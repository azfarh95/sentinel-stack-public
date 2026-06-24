@echo off
REM ════════════════════════════════════════════════════════════════════
REM  DEPRECATED 2026-06-17 — superseded by the Sentinel Watchdog.
REM
REM  Teardown (RAM-reclaim Stop-all) is now OWNED by the watchdog. It stops
REM  every app stack across ALL compose projects but PRESERVES the access
REM  plane (caddy-tailnet / caddy-headscale / headscale LAN bridge /
REM  cloudflared / the watchdog itself) so you never lose remote control of
REM  the box — a guarantee this .bat did not have (CF-530 lockout, 2026-06-17).
REM
REM  To stop the stack:
REM    * click "Stop stack" in the watchdog dashboard / Mini App, or
REM    * POST /api/v2/stack/stop with a service token, e.g.:
REM        curl -X POST -H "X-Sentinel-Service-Token: <token>" ^
REM             http://127.0.0.1:8200/api/v2/stack/stop
REM
REM  The original full-teardown script is in git history (this path, before
REM  the 2026-06-17 deprecation commit).
REM ════════════════════════════════════════════════════════════════════
echo.
echo  STOP_AI_STACK.bat is DEPRECATED - the watchdog now owns teardown.
echo  Use "Stop stack" in the watchdog dashboard / Mini App, or:
echo    curl -X POST -H "X-Sentinel-Service-Token: ^<token^>" http://127.0.0.1:8200/api/v2/stack/stop
echo.
