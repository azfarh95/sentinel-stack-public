@echo off
REM Launches Chromium with a known CDP debug port so:
REM  - Playwright MCP can attach via --cdp-endpoint (no separate browser)
REM  - Sentinel bridge can subscribe to Page.startScreencast for high-fps stream
REM  - Sentinel bridge can dispatch Input.dispatchMouseEvent / KeyEvent / ScrollEvent
REM
REM Persistent user-data-dir means cookies, login state, etc. survive restarts.

setlocal

set CHROMIUM_PROFILE_DIR=%LOCALAPPDATA%\sentinel-chromium-profile
set CDP_PORT=9222

REM Find the Playwright-bundled Chromium first (preferred — matches what Playwright expects)
set CHROME_EXE=
for /D %%d in ("%LOCALAPPDATA%\ms-playwright\chromium-*") do set CHROME_EXE=%%d\chrome-win\chrome.exe
if not exist "%CHROME_EXE%" (
    REM Fall back to system Chrome
    if exist "%PROGRAMFILES%\Google\Chrome\Application\chrome.exe" set CHROME_EXE=%PROGRAMFILES%\Google\Chrome\Application\chrome.exe
)
if not exist "%CHROME_EXE%" (
    if exist "%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe" set CHROME_EXE=%PROGRAMFILES(X86)%\Google\Chrome\Application\chrome.exe
)

if not exist "%CHROME_EXE%" (
    echo [chromium-cdp] ERROR: No Chromium binary found. Install Chrome or run: npx playwright install chromium
    exit /b 1
)

REM Make profile dir if missing
if not exist "%CHROMIUM_PROFILE_DIR%" mkdir "%CHROMIUM_PROFILE_DIR%"

echo [chromium-cdp] Launching: %CHROME_EXE%
echo [chromium-cdp] Profile: %CHROMIUM_PROFILE_DIR%
echo [chromium-cdp] CDP port: %CDP_PORT%

REM Launch flags:
REM  --remote-debugging-port      : exposes CDP HTTP+WS at /json/version
REM  --remote-debugging-address   : restrict to loopback only
REM  --user-data-dir              : persistent profile
REM  --no-first-run               : skip welcome screen
REM  --no-default-browser-check   : skip default-browser prompt
REM  --disable-features=...       : reduce noise
REM v3.5.2 stealth flags (helps against mild bot detection — NOT Cloudflare Turnstile)
REM  --disable-blink-features=AutomationControlled : suppresses navigator.webdriver
REM  --exclude-switches=enable-automation         : hides "Chrome is being controlled" infobar
"%CHROME_EXE%" ^
    --remote-debugging-port=%CDP_PORT% ^
    --remote-debugging-address=127.0.0.1 ^
    --remote-allow-origins=* ^
    --user-data-dir="%CHROMIUM_PROFILE_DIR%" ^
    --no-first-run ^
    --no-default-browser-check ^
    --disable-features=Translate,InfoBars,AutomationControlled ^
    --disable-blink-features=AutomationControlled ^
    --disable-popup-blocking ^
    about:blank
