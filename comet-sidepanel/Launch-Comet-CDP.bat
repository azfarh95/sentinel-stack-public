@echo off
REM Double-click wrapper for Launch-Comet-CDP.ps1.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Launch-Comet-CDP.ps1" %*
