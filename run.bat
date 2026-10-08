@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo FaceGotcha is not set up yet. Run setup.bat first.
    pause
    exit /b 1
)

echo Starting FaceGotcha at http://127.0.0.1:5000  (press Ctrl+C to stop)
".venv\Scripts\python.exe" app.py
pause
