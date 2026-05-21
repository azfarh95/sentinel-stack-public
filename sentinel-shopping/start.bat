@echo off
REM Launches sentinel-shopping-mcp from its own venv. Same pattern as
REM infer_bridge.py / sentinel-miniapp-v2/bridge.py — watchdog and
REM START_AI_STACK.bat invoke this (or just call python directly).
REM
REM Manual use: just double-click, or `cd %~dp0 && start.bat`.
"%~dp0.venv\Scripts\python.exe" -u "%~dp0mcp_server.py"
