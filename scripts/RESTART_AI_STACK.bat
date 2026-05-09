@echo off
setlocal
title Restarting AI Stack
color 0E

echo.
echo ================================================
echo   AI STACK RESTART
echo ================================================
echo.

REM ── Phase 1: Stop ────────────────────────────────
echo   Phase 1/3: Stopping all services...
echo.
set NOPAUSE=1
call "C:\Users\azfar\metamcp-local\scripts\STOP_AI_STACK.bat"

REM ── Phase 2: Confirm clean + countdown ───────────
echo.
echo   Phase 2/3: Confirming clean stop...

REM Poll until the three critical ports are gone (max 60s)
set /a WAIT_SECS=0
:confirm_loop
timeout /t 3 /nobreak >nul
set /a WAIT_SECS+=3
set ALL_CLEAR=1

netstat -ano 2>nul | findstr ":12008 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 set ALL_CLEAR=0

netstat -ano 2>nul | findstr ":1234 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 set ALL_CLEAR=0

netstat -ano 2>nul | findstr ":8095 " | findstr "LISTENING" >nul 2>&1
if %ERRORLEVEL% EQU 0 set ALL_CLEAR=0

if %ALL_CLEAR% EQU 1 goto confirmed_clean
if %WAIT_SECS% LSS 60 goto confirm_loop

echo        WARNING: Some ports still active after 60s - continuing anyway.
goto countdown

:confirmed_clean
echo        All critical ports clear (took %WAIT_SECS%s).

:countdown
echo.
echo   Waiting 60 seconds before restart...
echo.
for /L %%i in (60,-1,1) do (
    echo   Restarting in %%i...
    timeout /t 1 /nobreak >nul
)

REM ── Phase 3: Start ───────────────────────────────
echo.
echo   Phase 3/3: Starting all services...
echo.
set NOPAUSE=
call "C:\Users\azfar\metamcp-local\scripts\START_AI_STACK.bat"

endlocal
