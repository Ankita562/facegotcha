@echo off
setlocal
cd /d "%~dp0"

echo === FaceGotcha setup ===

REM 1. Python check (3.10 or newer)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo.
    echo Python 3.10 or newer was not found.
    echo Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" in the installer, then run setup.bat again.
    echo.
    pause
    exit /b 1
)

REM 2. Virtual environment
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 goto :fail
) else (
    echo Virtual environment already exists, reusing it.
)

REM 3. Dependencies (uses the venv's own Python, so no activation step is needed)
echo Installing packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul 2>&1
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :fail

REM 4. Models
echo Downloading models...
".venv\Scripts\python.exe" download_models.py
if errorlevel 1 goto :fail

echo.
echo Setup complete. Double-click run.bat to start FaceGotcha.
echo.
pause
exit /b 0

:fail
echo.
echo Setup failed. Scroll up to see the error, fix it, then run setup.bat again.
echo.
pause
exit /b 1
