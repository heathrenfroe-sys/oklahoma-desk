@echo off
rem Scheduled-task runner for edgar_watch.py -- %~dp0 is this file's folder,
rem so the task works no matter what directory Task Scheduler starts us in.
cd /d "%~dp0"
if not exist logs mkdir logs
echo [%date% %time%] edgar_watch run >> "logs\edgar_watch.log"
".venv\Scripts\python.exe" edgar_watch.py >> "logs\edgar_watch.log" 2>&1
".venv\Scripts\python.exe" market_pull.py >> "logs\edgar_watch.log" 2>&1
".venv\Scripts\python.exe" dashboard.py >> "logs\edgar_watch.log" 2>&1
