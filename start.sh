#!/bin/bash
# 命令行启动（macOS / Linux 通用）：./start.sh
# 首次运行会自动建虚拟环境、装依赖，然后起服务并打开浏览器。
set -e
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "未检测到 python3，请先安装 Python 3.9 以上版本。" >&2
  exit 1
fi

# 虚拟环境目录两种命名都认，与 update.sh 保持一致：优先 .venv（脚本自己建的那个），
# 其次手建的 venv。不这样的话，手工建过 venv 的机器会再建一个 .venv 白装一遍依赖。
VENV=""
for c in .venv venv; do
  if [ -x "$c/bin/python" ]; then VENV="$c"; break; fi
done
if [ -z "$VENV" ]; then
  VENV=.venv
  echo "首次运行，正在创建虚拟环境（.venv）…"
  python3 -m venv "$VENV"
fi

if ! "$VENV/bin/python" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  echo "正在安装依赖…"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q fastapi "uvicorn[standard]"
fi

if [ ! -f data/law_data.db ]; then
  echo ""
  echo "提示：还没有数据文件 data/law_data.db"
  echo "      先运行这一行下载（约 676 MB；装过 zstd 则是 514 MB）："
  echo "        python3 tools/fetch_data.py"
  echo "      服务仍会启动，但查不到任何条文。"
  echo ""
fi

export LAW_API_ENFORCE=${LAW_API_ENFORCE:-0}
export LAW_HOST=${LAW_HOST:-127.0.0.1}

# 8001 被占用时顺延，避免「端口已占用」直接起不来
PORT=${LAW_PORT:-8001}
while lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 && [ $PORT -lt 8010 ]; do
  PORT=$((PORT + 1))
done
export LAW_PORT=$PORT

URL="http://${LAW_HOST}:${PORT}"
echo "本地查询界面：${URL}"
echo "按 Ctrl+C 停止服务。"

# 等服务就绪再开浏览器，避免打开就是「无法连接」
(
  for _ in $(seq 1 40); do
    sleep 0.5
    if command -v curl >/dev/null 2>&1; then
      curl -sf -o /dev/null --noproxy '*' "${URL}/health" && break
    else
      sleep 2 && break
    fi
  done
  if command -v open >/dev/null 2>&1; then
    open "$URL" 2>/dev/null || true          # macOS
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 || true  # Linux
  fi
) &

exec "$VENV/bin/python" -m uvicorn server.law_search_service:app --host "$LAW_HOST" --port "$PORT"
