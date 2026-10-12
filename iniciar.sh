#!/usr/bin/env bash
cd "$(dirname "$0")"
set -e
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers"
.venv/bin/python -m pip install -q -r requirements.txt
if ! .venv/bin/python -m playwright install chromium; then
  echo "Chromium no se pudo instalar. El panel puede arrancar; Maps sin API requerira completar la instalacion."
fi
.venv/bin/python arrancar.py "$@"
