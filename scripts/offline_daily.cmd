@echo off
rem Daily XinwenLianbo report - offline fallback runner (called by Task Scheduler).
rem Runs at 21:35; skips automatically if the day's report was already sent (marker).
cd /d "D:\CTYJ\DeepSeek\Harness\News"
"D:\CTYJ\Agent\Python\python.exe" scripts\standalone_report.py >> logs\scheduler.log 2>&1
exit /b %errorlevel%
