@echo off
rem Codex + Claude: starts the local browser app (127.0.0.1 only). Keep this window open while a task runs.
title Codex + Claude
chcp 65001 >nul
set PYTHONUTF8=1
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% -B orchestrator.py app %*
echo.
pause
