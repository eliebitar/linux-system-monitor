@echo off
REM Start SysMon on Windows. Uses venv\Scripts\python.exe if a venv exists,
REM otherwise falls back to the python on PATH.
cd /d "%~dp0"
echo Starting SysMon...
if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" server.py
) else (
    python server.py
)
