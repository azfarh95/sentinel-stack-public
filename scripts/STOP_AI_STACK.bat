@echo off
setlocal
title Stopping AI Stack
color 0C

echo.
echo ================================================
echo   AI STACK SHUTDOWN
echo ================================================
echo.

REM ── 1. LM Studio ─────────────────────────────────
echo [1/6] Stopping LM Studio...
taskkill /IM "LM Studio.exe" /F >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    echo        OK - LM Studio stopped
) else (
    echo        SKIPPED - LM Studio was not running
)

REM ── 2. Playwright MCP (ports 8931 / 8932) ────────
echo [2/6] Stopping Playwright MCP...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8931 " ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8932 " ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
echo        OK

REM ── 2b. Inference Bridge (port 8095) ─────────────
echo [2b] Stopping Inference Bridge...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8095 " ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
echo        OK

REM ── 2c. Sentinel Bridge (port 8098) ──────────────
echo [2c] Stopping Sentinel Bridge...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8098 " ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
echo        OK

REM ── 2cc. Shopping MCP (port 8100) ────────────────
echo [2cc] Stopping Shopping MCP...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8100 " ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
echo        OK

REM ── 2ce. OpenClaw Sidepanel Bridge (port 8101) ───
echo [2ce] Stopping OpenClaw Sidepanel Bridge...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8101 " ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
echo        OK

REM ── 2cf. Comet-Sidepanel MCP (port 8102) ─────────
echo [2cf] Stopping Comet-Sidepanel MCP...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8102 " ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
)
echo        OK

REM ── 2d. Cloudflare tunnel (named service) ────────
echo [2d] Stopping Cloudflare tunnel...
sc stop cloudflared >nul 2>&1
echo        OK

REM ── 3. OpenClaw (WSL2) ───────────────────────────
echo [3/6] Stopping OpenClaw (WSL2)...
wsl -d Ubuntu-24.04 -u root -- bash -c "systemctl is-active --quiet openclaw-gateway.service" >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    wsl -d Ubuntu-24.04 -u root -- bash -c "systemctl stop openclaw-gateway.service" >nul 2>&1
    echo        OK - OpenClaw stopped
) else (
    echo        SKIPPED - OpenClaw was not running
)

REM ── 4. Docker containers ─────────────────────────
echo [4/6] Stopping Docker containers...
cd /d "%~dp0.."
REM Consolidated docker-compose.yml — single down covers all profiles
docker compose --env-file .env.local --profile media --profile finance down
if %ERRORLEVEL% EQU 0 (
    echo        OK - All containers stopped
) else (
    echo        SKIPPED - containers were not running
)

REM (smdl + Firefly are now in the consolidated docker-compose.yml under
REM `media` and `finance` profiles — handled by the single down above.)

REM ── 4d. Clear START lock file if stale ───────────
if exist "%TEMP%\ai_stack_start.lock" (
    del "%TEMP%\ai_stack_start.lock" >nul 2>&1
    echo        Cleared stale START lock file
)

REM ── 5. WSL2 keepalive ────────────────────────────
echo [5/6] Stopping WSL2 keepalive process...
set KEEPALIVE_PID_FILE=%TEMP%\wsl_keepalive.pid
if exist "%KEEPALIVE_PID_FILE%" (
    set /p KEEPALIVE_PID=<"%KEEPALIVE_PID_FILE%"
    taskkill /PID %KEEPALIVE_PID% /F >nul 2>&1
    del "%KEEPALIVE_PID_FILE%" >nul 2>&1
)
REM Window title fallback no longer needed (keepalive runs headless via PowerShell)
echo        OK

REM ── 6. Verify nothing is still running ───────────
echo [6/6] Verifying clean shutdown...
timeout /t 3 /nobreak >nul

set CLEAN=1
netstat -ano 2>nul | findstr ":12008 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 ( echo        WARN: port 12008 still in use && set CLEAN=0 )

netstat -ano 2>nul | findstr ":8089 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 ( echo        WARN: port 8089 still in use && set CLEAN=0 )

REM port 18789 (OpenClaw) stays bound inside WSL - not checked here

netstat -ano 2>nul | findstr ":1234 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 ( echo        WARN: port 1234 still in use && set CLEAN=0 )

netstat -ano 2>nul | findstr ":8095 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 ( echo        WARN: port 8095 (inference bridge) still in use && set CLEAN=0 )

netstat -ano 2>nul | findstr ":8097 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 ( echo        WARN: port 8097 (sentinel bridge) still in use && set CLEAN=0 )

netstat -ano 2>nul | findstr ":8101 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 ( echo        WARN: port 8101 (openclaw sidepanel bridge) still in use && set CLEAN=0 )

netstat -ano 2>nul | findstr ":8102 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 ( echo        WARN: port 8102 (comet-sidepanel mcp) still in use && set CLEAN=0 )

if %CLEAN%==1 ( echo        All ports clear ) else ( echo        Some ports still active - may need manual cleanup )

echo.
echo ================================================
echo   NOTE: Docker Desktop is still running.
echo   Right-click its tray icon ^> Quit Docker Desktop
echo   for a full system shutdown.
echo ================================================
echo.
echo   AI Stack stopped cleanly.
echo.
if not defined NOPAUSE pause
endlocal
