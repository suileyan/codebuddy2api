#!/usr/bin/env bash
# workbuddy2api 本地原生启动（bash）
set -euo pipefail
cd "$(dirname "$0")/../.."

if [ -x ".venv/Scripts/python.exe" ]; then
  PY=".venv/Scripts/python.exe"
elif [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

exec "$PY" deploy/local/serve.py
