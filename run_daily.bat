@echo off
REM Blog auto-publish daily cron
REM Triggered by Windows Task Scheduler "BlogAutoDaily" at 03:00 KST
REM Actual publish time randomized 03:00~07:00 to look natural (people post in morning, not exact 03:00)
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
REM Random sleep 0~14400 seconds (0~4 hours)
powershell -NoProfile -Command "Start-Sleep -Seconds (Get-Random -Minimum 0 -Maximum 14400)"
python pipeline.py --publish >> _logs\daily.log 2>&1
exit /b %ERRORLEVEL%
