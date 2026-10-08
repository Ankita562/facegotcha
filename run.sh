#!/usr/bin/env bash
cd "$(dirname "$0")"

if [ -x ".venv/bin/python" ]; then
    VENV_PY=".venv/bin/python"
elif [ -x ".venv/Scripts/python.exe" ]; then
    VENV_PY=".venv/Scripts/python.exe"
else
    echo "FaceGotcha is not set up yet. Run ./setup.sh first."
    exit 1
fi

echo "Starting FaceGotcha at http://127.0.0.1:5000  (press Ctrl+C to stop)"
exec "$VENV_PY" app.py
