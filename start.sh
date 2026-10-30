#!/bin/bash
# 命令行启动（macOS / Linux 通用）：./start.sh
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q fastapi "uvicorn[standard]"

export LAW_API_ENFORCE=${LAW_API_ENFORCE:-0}
export LAW_HOST=${LAW_HOST:-127.0.0.1}
export LAW_PORT=${LAW_PORT:-8001}

echo "本地服务：http://${LAW_HOST}:${LAW_PORT}"
exec .venv/bin/python -m uvicorn server.law_search_service:app --host "$LAW_HOST" --port "$LAW_PORT"
