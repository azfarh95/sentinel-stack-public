@echo off
title Playwright MCP Server
echo Starting Playwright MCP Server on port 8931...
echo Browser: Chrome (visible window)
echo MetaMCP connects via http://host.docker.internal:8931/sse
echo.
echo Keep this window open while using browser tools in LM Studio.
echo Close this window to stop the Playwright MCP server.
echo.
REM V3.4: attach to our externally-launched Chromium (with CDP exposed) so the
REM bridge's screencast subscription and the agent's tool calls drive the SAME
REM browser. start_chromium_cdp.bat must be running first; if Chromium is not
REM reachable on :9222, Playwright MCP will fail to start (intentional — we
REM want the failure to be loud, not silent fallback).
REM --shared-browser-context kept for redundancy; the cdp-endpoint already
REM provides single-context behaviour.
npx @playwright/mcp@latest --port 8931 --cdp-endpoint http://127.0.0.1:9222 --shared-browser-context
pause
