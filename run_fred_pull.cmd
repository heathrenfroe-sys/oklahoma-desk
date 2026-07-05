@echo off
rem Scheduled-task runner for fred_pull.py -- Fridays after the rig count.
cd /d "%~dp0"
if not exist logs mkdir logs
echo [%date% %time%] fred_pull run >> "logs\fred_pull.log"
".venv\Scripts\python.exe" fred_pull.py >> "logs\fred_pull.log" 2>&1
".venv\Scripts\python.exe" dashboard.py >> "logs\fred_pull.log" 2>&1
