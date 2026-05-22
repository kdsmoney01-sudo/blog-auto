@echo off
REM Blog auto-publish daily cron — 03:00 KST
REM Triggered by Windows Task Scheduler "BlogAutoDaily"
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
python pipeline.py --publish >> _logs\daily.log 2>&1
exit /b %ERRORLEVEL%
