@echo off
rem Scheduled-task runner for fdic_pull.py -- monthly; the script warns if
rem the FDIC hasn't ingested the newest quarter for every bank yet.
cd /d "%~dp0"
if not exist logs mkdir logs
echo [%date% %time%] fdic_pull run >> "logs\fdic_pull.log"
".venv\Scripts\python.exe" fdic_pull.py >> "logs\fdic_pull.log" 2>&1
".venv\Scripts\python.exe" dashboard.py >> "logs\fdic_pull.log" 2>&1
