#!/usr/bin/env sh
set -e
PORT="${PORT:-8765}"
exec uvicorn main:app --host 0.0.0.0 --port "$PORT"
