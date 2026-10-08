#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

echo "=== FaceGotcha setup ==="

# Pick a Python command
if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "Python was not found. Install Python 3.10+ from https://www.python.org/downloads/"
    exit 1
fi

# Check version (3.10 or newer)
if ! "$PY" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"; then
    echo "Python 3.10 or newer is required."
    exit 1
fi

# Virtual environment (Git Bash on Windows uses Scripts/, macOS/Linux use bin/)
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    "$PY" -m venv .venv
else
    echo "Virtual environment already exists, reusing it."
fi

if [ -x ".venv/bin/python" ]; then
    VENV_PY=".venv/bin/python"
else
    VENV_PY=".venv/Scripts/python.exe"
fi

echo "Installing packages..."
"$VENV_PY" -m pip install --upgrade pip >/dev/null 2>&1 || true
"$VENV_PY" -m pip install -r requirements.txt

echo "Downloading models..."
"$VENV_PY" download_models.py

echo
echo "Setup complete. Run ./run.sh to start FaceGotcha."
