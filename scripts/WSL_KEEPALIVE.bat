@echo off
REM WSL2 keepalive — keeps Ubuntu-24.04 distro alive so system services don't die.
REM Loops with retry in case the wsl session drops briefly.
:loop
wsl -d Ubuntu-24.04 -u root sleep infinity
timeout /t 10 /nobreak >nul
goto loop
