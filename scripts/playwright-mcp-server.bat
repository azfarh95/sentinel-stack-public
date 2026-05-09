@echo off
title Playwright MCP Server
echo Starting Playwright MCP Server on port 8931...
echo Browser: Chrome (visible window)
echo MetaMCP connects via http://host.docker.internal:8931/sse
echo.
echo Keep this window open while using browser tools in LM Studio.
echo Close this window to stop the Playwright MCP server.
echo.
REM --shared-browser-context: lets multiple HTTP clients (agent + bridge for the
REM mini app browser panel) share one browser context. Without this, each MCP
REM session locks the single Chromium and they fight for it.
npx @playwright/mcp@latest --port 8931 --browser chrome --shared-browser-context
pause
