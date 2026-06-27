#!/usr/bin/env bash
# Start the POPOP production pipeline server.
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8077}"
HOST="${HOST:-127.0.0.1}"

# install deps on first run
python3 -c "import fastapi, uvicorn, requests, PIL, multipart" 2>/dev/null || \
  python3 -m pip install --user -r requirements.txt

echo "POPOP pipeline -> http://${HOST}:${PORT}"
PYTHONPATH="$(pwd)" python3 -m uvicorn app.server:app --host "$HOST" --port "$PORT" --reload
