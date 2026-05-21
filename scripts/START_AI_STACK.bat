@echo off
setlocal
title Starting AI Stack
color 0A

REM ── Singleton guard: prevent multiple instances ───
set LOCKFILE=%TEMP%\ai_stack_start.lock
if exist "%LOCKFILE%" (
    REM Check if any START_AI_STACK window is actually still open
    tasklist /FI "WINDOWTITLE eq Starting AI Stack" 2>nul | findstr "cmd.exe" >nul 2>&1
    if %ERRORLEVEL% EQU 0 (
        echo.
        echo  WARNING: START_AI_STACK is already running.
        echo  Close the other window first, then retry.
        echo.
        pause
        exit /b 1
    )
    REM Lock is stale - clear it and continue
    echo  INFO: Clearing stale lock file and continuing...
    del "%LOCKFILE%" >nul 2>&1
)
echo %TIME% > "%LOCKFILE%"

echo.
echo ================================================
echo   AI STACK STARTUP
echo ================================================
echo.

REM ── 0. Sync .env.local from Windows Credential Manager ───
echo [0/7] Syncing secrets from Windows Credential Manager...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0sync_env_from_wcm.ps1" 2>&1 | findstr /v /c:"PSReadLine" /c:"^$"
if %ERRORLEVEL% NEQ 0 (
    echo        WARN: Secret sync failed — .env.local may be stale.
    echo        Continuing with existing .env.local; rotate secrets via WCM and re-run.
)

REM ── 1. Docker Desktop ────────────────────────────
echo [1/7] Checking Docker Desktop...
docker info >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo        OK - Docker already running
    goto docker_ready
)
echo        Starting Docker Desktop...
start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
echo        Waiting for Docker daemon (up to 90s)...
:docker_wait
timeout /t 5 /nobreak >nul
docker info >nul 2>&1
if %ERRORLEVEL% NEQ 0 goto docker_wait
:docker_ready
echo        Docker ready

REM ── 2. Docker containers ─────────────────────────
echo [2/7] Starting Docker containers...
cd /d "%~dp0.."
REM Consolidated docker-compose.yml uses profiles (V6 prep, 2026-05-10).
REM   default services: metamcp + postgres + 9 MCP servers (always on)
REM   media profile:    smdl
REM   finance profile:  firefly + firefly-db (now always-on for auto-import schedule)
docker compose --env-file .env.local --profile media --profile finance up -d
echo.

echo        Waiting for MetaMCP to be healthy...
:metamcp_wait
timeout /t 5 /nobreak >nul
for /f %%s in ('docker inspect metamcp --format "{{.State.Health.Status}}" 2^>nul') do set MSTATUS=%%s
if not "%MSTATUS%"=="healthy" goto metamcp_wait
echo        MetaMCP ready (port 12008)

echo        Waiting for Google Workspace MCP...
:gwmcp_wait
timeout /t 3 /nobreak >nul
for /f %%s in ('docker inspect google-workspace-mcp --format "{{.State.Health.Status}}" 2^>nul') do set GSTATUS=%%s
if not "%GSTATUS%"=="healthy" goto gwmcp_wait
echo        Google Workspace MCP ready (port 8089)

echo        Waiting for yt-dlp MCP...
:ytdlp_wait
timeout /t 3 /nobreak >nul
for /f %%s in ('docker inspect ytdlp-mcp --format "{{.State.Health.Status}}" 2^>nul') do set YSTATUS=%%s
if not "%YSTATUS%"=="healthy" goto ytdlp_wait
echo        yt-dlp MCP ready (port 8088)

echo        Waiting for Reminders MCP...
:reminders_wait
timeout /t 3 /nobreak >nul
for /f %%s in ('docker inspect reminders-mcp --format "{{.State.Health.Status}}" 2^>nul') do set RSTATUS=%%s
if not "%RSTATUS%"=="healthy" goto reminders_wait
echo        Reminders MCP ready (port 8087)

echo        Waiting for Firefly III...
:firefly_wait
timeout /t 5 /nobreak >nul
for /f %%s in ('docker inspect firefly --format "{{.State.Health.Status}}" 2^>nul') do set FFSTATUS=%%s
if not "%FFSTATUS%"=="healthy" goto firefly_wait
echo        Firefly III ready (port 8180)

REM ── 3. OpenClaw (WSL2 Ubuntu-24.04) ──────────────
echo [3/7] Checking OpenClaw in WSL2...

REM Keep WSL2 distro alive — WSL2 shuts down when all wsl.exe exit, killing system services.
REM A hidden background wsl process prevents that shutdown. PID saved to %TEMP% for STOP script.
set KEEPALIVE_PID_FILE=%TEMP%\wsl_keepalive.pid
if exist "%KEEPALIVE_PID_FILE%" (
    set /p OLD_PID=<"%KEEPALIVE_PID_FILE%"
    tasklist /FI "PID eq %OLD_PID%" 2>nul | findstr "wsl.exe" >nul 2>&1
    if %ERRORLEVEL% EQU 0 goto keepalive_running
)
powershell -NoProfile -WindowStyle Hidden -Command "$p = Start-Process wsl -ArgumentList '-d','Ubuntu-24.04','-u','root','sleep','infinity' -PassThru -WindowStyle Hidden; [System.IO.File]::WriteAllText('%KEEPALIVE_PID_FILE%', $p.Id.ToString())"
timeout /t 2 /nobreak >nul
:keepalive_running

wsl -d Ubuntu-24.04 -u root -- bash -c "systemctl is-active --quiet openclaw-gateway.service" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo        OK - OpenClaw already running
    goto openclaw_ready
)
echo        Starting OpenClaw...
wsl -d Ubuntu-24.04 -u root -- bash -c "systemctl start openclaw-gateway.service" >nul 2>&1
:openclaw_wait
timeout /t 3 /nobreak >nul
wsl -d Ubuntu-24.04 -u root -- bash -c "systemctl is-active --quiet openclaw-gateway.service" >nul 2>&1
if %ERRORLEVEL% NEQ 0 goto openclaw_wait
:openclaw_ready
echo        OpenClaw ready (WSL2 port 18789)

REM ── 4. LM Studio ─────────────────────────────────
echo [4/7] Checking LM Studio...
netstat -ano 2>nul | findstr ":1234 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo        OK - LM Studio already running
    goto lmstudio_ready
)
echo        Starting LM Studio...
start "" "%LOCALAPPDATA%\Programs\LM Studio\LM Studio.exe"
echo        Waiting for LM Studio API (port 1234)...
:lmstudio_wait
timeout /t 5 /nobreak >nul
netstat -ano 2>nul | findstr ":1234 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% NEQ 0 goto lmstudio_wait
:lmstudio_ready
echo        LM Studio ready (port 1234)

REM ── 5. Playwright MCP ────────────────────────────
echo [5/7] Starting Playwright MCP watcher...
netstat -ano 2>nul | findstr ":8932 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo        OK - Playwright proxy already running (port 8932)
    goto playwright_ready
)
schtasks /Run /TN "Playwright MCP Watcher" >nul 2>&1
set PLAYWRIGHT_TRIES=0
:playwright_wait
timeout /t 5 /nobreak >nul
set /a PLAYWRIGHT_TRIES+=1
netstat -ano 2>nul | findstr ":8932 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo        Playwright MCP ready (port 8932)
    goto playwright_ready
)
if %PLAYWRIGHT_TRIES% LSS 18 goto playwright_wait
echo        WARN - Playwright proxy (port 8932) did not start within 90s
echo        MetaMCP may fail to connect to Playwright tools
:playwright_ready

REM ── 6. Inference Bridge ──────────────────────────
echo [6/7] Starting Inference Bridge...
netstat -ano 2>nul | findstr ":8095 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo        OK - Inference bridge already running (port 8095)
    goto infer_bridge_ready
)
start "" /B "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" "%~dp0..\infer_bridge.py"
set INFER_TRIES=0
:infer_bridge_wait
timeout /t 2 /nobreak >nul
set /a INFER_TRIES+=1
netstat -ano 2>nul | findstr ":8095 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo        Inference bridge ready (port 8095)
    goto infer_bridge_ready
)
if %INFER_TRIES% LSS 10 goto infer_bridge_wait
echo        WARN - Inference bridge did not start — spikes will be tagged as abnormal
:infer_bridge_ready

REM ── 6b. Sentinel Bridge ───────────────────────────
echo [6b] Starting Sentinel Bridge...
netstat -ano 2>nul | findstr ":8098 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo        OK - Sentinel bridge already running (port 8098)
    goto sentinel_bridge_ready
)
start "" /B "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" "%~dp0..\sentinel-miniapp-v2\bridge.py"
set SENTINEL_TRIES=0
:sentinel_bridge_wait
timeout /t 2 /nobreak >nul
set /a SENTINEL_TRIES+=1
netstat -ano 2>nul | findstr ":8098 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo        Sentinel bridge ready (port 8098)
    goto sentinel_bridge_ready
)
if %SENTINEL_TRIES% LSS 5 goto sentinel_bridge_wait
echo        WARN - Sentinel bridge did not start
:sentinel_bridge_ready

REM ── 6cd. OpenClaw Sidepanel Bridge (port 8101) ───
REM HTTP shim for the Comet sidepanel extension. Calls `wsl openclaw agent`
REM per /chat request. Port 8101 because 8090-8100 are Hyper-V reserved.
echo [6cd] Starting OpenClaw Sidepanel Bridge...
netstat -ano 2>nul | findstr ":8101 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo        OK - OpenClaw bridge already running (port 8101)
    goto openclaw_bridge_ready
)
start "" /B "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" "%~dp0..\comet-sidepanel\bridge.py"
set OCBRIDGE_TRIES=0
:openclaw_bridge_wait
timeout /t 2 /nobreak >nul
set /a OCBRIDGE_TRIES+=1
netstat -ano 2>nul | findstr ":8101 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo        OpenClaw bridge ready (port 8101)
    goto openclaw_bridge_ready
)
if %OCBRIDGE_TRIES% LSS 5 goto openclaw_bridge_wait
echo        WARN - OpenClaw bridge did not start — Comet sidepanel will say "bridge offline"
:openclaw_bridge_ready

REM ── 6ce. Comet-Sidepanel MCP (port 8102) ─────────
REM Self-introspection MCP server exposed to MetaMCP Default namespace.
REM Tools: bridge_health, bridge_audit_tail, comet_cdp_status,
REM playwright_mcp_status, describe_architecture.
echo [6ce] Starting Comet-Sidepanel MCP server...
netstat -ano 2>nul | findstr ":8102 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo        OK - Comet-Sidepanel MCP already running (port 8102)
    goto comet_mcp_ready
)
start "" /B "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" "%~dp0..\comet-sidepanel\mcp_server.py"
set OCMCP_TRIES=0
:comet_mcp_wait
timeout /t 2 /nobreak >nul
set /a OCMCP_TRIES+=1
netstat -ano 2>nul | findstr ":8102 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo        Comet-Sidepanel MCP ready (port 8102)
    goto comet_mcp_ready
)
if %OCMCP_TRIES% LSS 5 goto comet_mcp_wait
echo        WARN - Comet-Sidepanel MCP did not start
:comet_mcp_ready

REM ── 6d. Shopping MCP ──────────────────────────────
REM Native Python in its own venv. Same boot pattern as Sentinel Bridge.
echo [6d] Starting Shopping MCP...
netstat -ano 2>nul | findstr ":8100 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo        OK - Shopping MCP already running (port 8100)
    goto shopping_mcp_ready
)
if exist "%USERPROFILE%\sentinel-shopping\.venv\Scripts\pythonw.exe" (
    start "" /D "%USERPROFILE%\sentinel-shopping" /B "%USERPROFILE%\sentinel-shopping\.venv\Scripts\pythonw.exe" "%USERPROFILE%\sentinel-shopping\mcp_server.py"
) else (
    echo        WARN - venv pythonw not found at %USERPROFILE%\sentinel-shopping\.venv — skipping
    goto shopping_mcp_ready
)
set SHOPPING_TRIES=0
:shopping_mcp_wait
timeout /t 2 /nobreak >nul
set /a SHOPPING_TRIES+=1
netstat -ano 2>nul | findstr ":8100 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo        Shopping MCP ready (port 8100)
    goto shopping_mcp_ready
)
if %SHOPPING_TRIES% LSS 5 goto shopping_mcp_wait
echo        WARN - Shopping MCP did not start
:shopping_mcp_ready

REM ── 6c. Cloudflare Tunnel ─────────────────────────
echo [6c] Starting Cloudflare tunnel...
sc query cloudflared 2>nul | findstr "RUNNING" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo        OK - Cloudflare tunnel already running
) else (
    sc start cloudflared >nul 2>&1
    timeout /t 3 /nobreak >nul
    sc query cloudflared 2>nul | findstr "RUNNING" >nul 2>&1
    if %ERRORLEVEL% EQU 0 (
        echo        Cloudflare tunnel started
    ) else (
        echo        WARN - Cloudflare tunnel did not start
    )
)

REM ── 7. Verify all ports are alive ────────────────
REM Sleep before checking — Hyper-V vpnkit port bindings and Task Scheduler
REM spawns take a few seconds to settle even after their parent commands
REM return. Without this pause, the [7/8] check fires too early and produces
REM spurious WARNs even though everything actually came up fine.
echo [7/8] Final connectivity check (giving services 10s to settle)...
timeout /t 10 /nobreak >nul
set ALL_OK=1

netstat -ano 2>nul | findstr ":12008 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% NEQ 0 ( echo        WARN: MetaMCP port 12008 not responding && set ALL_OK=0 )

netstat -ano 2>nul | findstr ":8089 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% NEQ 0 ( echo        WARN: Google WS MCP port 8089 not responding && set ALL_OK=0 )

netstat -ano 2>nul | findstr ":8087 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% NEQ 0 ( echo        WARN: Reminders MCP port 8087 not responding && set ALL_OK=0 )

netstat -ano 2>nul | findstr ":8092 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% NEQ 0 ( echo        WARN: Memory MCP port 8092 not responding && set ALL_OK=0 )

netstat -ano 2>nul | findstr ":8932 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% NEQ 0 ( echo        WARN: Playwright proxy port 8932 not responding && set ALL_OK=0 )

netstat -ano 2>nul | findstr ":1234 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% NEQ 0 ( echo        WARN: LM Studio port 1234 not responding && set ALL_OK=0 )

netstat -ano 2>nul | findstr ":8095 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% NEQ 0 ( echo        WARN: Inference bridge port 8095 not responding && set ALL_OK=0 )

netstat -ano 2>nul | findstr ":8098 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% NEQ 0 ( echo        WARN: Sentinel bridge port 8098 not responding && set ALL_OK=0 )

netstat -ano 2>nul | findstr ":8100 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% NEQ 0 ( echo        WARN: Shopping MCP port 8100 not responding && set ALL_OK=0 )

netstat -ano 2>nul | findstr ":8101 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% NEQ 0 ( echo        WARN: OpenClaw Sidepanel Bridge port 8101 not responding && set ALL_OK=0 )

netstat -ano 2>nul | findstr ":8102 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% NEQ 0 ( echo        WARN: Comet-Sidepanel MCP port 8102 not responding && set ALL_OK=0 )

wsl -d Ubuntu-24.04 -u root -- bash -c "ss -tlnp 2>/dev/null | grep -q ':18789'" >nul 2>&1
if %ERRORLEVEL% NEQ 0 ( echo        WARN: OpenClaw port 18789 not responding && set ALL_OK=0 )

if %ALL_OK%==1 ( echo        All ports verified OK ) else ( echo        One or more services may need attention )

REM ── 8. Telegram quick-action keyboard ────────────
echo [8/8] Injecting Telegram quick-action keyboard...
py -3 "%~dp0keyboard_bot.py" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo        Keyboard + dashboard panel sent to Sentinel group
) else (
    echo        WARN: Keyboard send failed (check network or bot token)
)

echo.
echo ================================================
echo   AI STACK ONLINE
echo ------------------------------------------------
echo   MetaMCP          : http://127.0.0.1:12008
echo   Google WS MCP    : http://127.0.0.1:8089
echo   yt-dlp MCP       : http://127.0.0.1:8088
echo   Reminders MCP    : http://127.0.0.1:8087
echo   Memory MCP       : http://127.0.0.1:8092
echo   OpenClaw         : WSL2 port 18789
echo   LM Studio API    : http://127.0.0.1:1234
echo   Infer Bridge     : http://127.0.0.1:8095
echo   Sentinel Bridge  : http://127.0.0.1:8098
echo   Shopping MCP     : http://127.0.0.1:8100
echo   OpenClaw Bridge  : http://127.0.0.1:8101  (Comet sidepanel)
echo   Comet-Sidepanel MCP: http://127.0.0.1:8102/mcp
echo   Sentinel App     : https://your-domain.example.com
echo   Firefly III      : http://127.0.0.1:8180
echo   Telegram bot     : @YourSentinelBot
echo ================================================
echo.
echo   Use STOP_AI_STACK.bat for clean shutdown.
echo.
if not defined NOPAUSE pause
del "%LOCKFILE%" >nul 2>&1
endlocal
