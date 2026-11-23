#!/usr/bin/env bash
# Build standalone macOS / Linux executables with PyInstaller.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if [ ! -x ".venv/bin/python" ]; then
    "$PY" -m venv .venv
    .venv/bin/pip install -q --upgrade pip
    .venv/bin/pip install -q -r requirements-dev.txt
fi

.venv/bin/python tools/make_icon.py
COMMON=(--noconfirm --clean --onefile --add-data "assets:assets"
        --collect-data customtkinter --collect-all pillow_heif)

if [ "$(uname -s)" = "Darwin" ]; then
    .venv/bin/pyinstaller "${COMMON[@]}" --windowed --icon assets/icon.ico --name MediaOrganizer run_gui.py
    .venv/bin/pyinstaller "${COMMON[@]}" --console --name MediaOrganizer-cli run_cli.py
else
    .venv/bin/pyinstaller "${COMMON[@]}" --noconsole --name MediaOrganizer run_gui.py
    .venv/bin/pyinstaller "${COMMON[@]}" --console --name MediaOrganizer-cli run_cli.py
fi

ls -lh dist/
