#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV_DIR=".venv"
SETUP_MARKER="$VENV_DIR/.querycraft_setup_complete"
PYTHON_EXE="$VENV_DIR/bin/python"

choose_python() {
    local candidate
    for candidate in python3.11 python3.10 python3.9 python3; do
        if command -v "$candidate" >/dev/null 2>&1; then
            if "$candidate" -c 'import sys; raise SystemExit(0 if (3, 9) <= sys.version_info[:2] < (3, 12) else 1)' >/dev/null 2>&1; then
                printf '%s\n' "$candidate"
                return 0
            fi
        fi
    done
    return 1
}

if [[ ! -d "$VENV_DIR" ]]; then
    echo "[setup] Creating virtual environment..."
    if ! BOOTSTRAP_PYTHON="$(choose_python)"; then
        echo "[error] Python 3.9-3.11 is required by the current MeloTTS dependency set." >&2
        echo "[error] Install Python 3.11, then run ./run.sh again." >&2
        exit 1
    fi
    "$BOOTSTRAP_PYTHON" -m venv "$VENV_DIR"
fi

if [[ ! -x "$PYTHON_EXE" ]]; then
    echo "[error] Virtual environment is incomplete: $PYTHON_EXE was not found." >&2
    exit 1
fi

if [[ ! -f "$SETUP_MARKER" ]]; then
    echo "[setup] Installing Python packages..."
    "$PYTHON_EXE" -m pip install --upgrade pip setuptools wheel
    "$PYTHON_EXE" -m pip install -r requirements.txt
    "$PYTHON_EXE" -m pip install -r requirements_mcp.txt
    "$PYTHON_EXE" -m pip install -r requirements_korean.txt

    echo "[setup] Downloading UniDic dictionary..."
    "$PYTHON_EXE" -m unidic download

    touch "$SETUP_MARKER"
    echo "[setup] First-run setup complete."
fi

echo "[run] Starting QueryCraft..."
exec "$PYTHON_EXE" assistant.py
