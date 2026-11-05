#!/bin/bash
# 命令行启动（macOS / Linux 通用）：./start.sh
# 首次运行会自动建虚拟环境、装依赖，然后起服务并打开浏览器。
set -e
cd "$(dirname "$0")"

# 优先用已装的 3.10+ 解释器：系统自带的 python3 往往是 3.9，直接拿它建环境等于白装一遍。
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 &&
     "$c" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' >/dev/null 2>&1; then
    PY="$c"; break
  fi
done

# 虚拟环境目录两种命名都认，与 update.sh 保持一致：优先 .venv（脚本自己建的那个），
# 其次手建的 venv。不这样的话，手工建过 venv 的机器会再建一个 .venv 白装一遍依赖。
VENV=""
for c in .venv venv; do
  if [ -x "$c/bin/python" ]; then VENV="$c"; break; fi
done
if [ -z "$VENV" ]; then
  if [ -z "$PY" ]; then
    if command -v python3 >/dev/null 2>&1; then
      python3 tools/check_python.py
    else
      echo "未检测到 python3。本服务需要 Python 3.10 或更高版本，安装方式见 INSTALL.md 第 3.1 节。" >&2
    fi
    exit 1
  fi
  VENV=.venv
  echo "首次运行，正在创建虚拟环境（.venv，$("$PY" -V 2>&1)）…"
  "$PY" -m venv "$VENV" || { echo "创建虚拟环境失败：$VENV" >&2; exit 1; }
fi

# 已有的 venv 可能是用 3.9 建的，再核一次，避免 uvicorn import 时抛一段栈
"$VENV/bin/python" tools/check_python.py || exit 1

if ! "$VENV/bin/python" -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  echo "正在安装依赖…"
  "$VENV/bin/pip" install -q --upgrade pip || true   # 升级 pip 失败不影响安装，别让它打断首次安装
  if ! "$VENV/bin/pip" install -q fastapi "uvicorn[standard]"; then
    echo "依赖安装失败，多半是访问 PyPI 超时。可以换镜像重试：" >&2
    echo "  PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ ./start.sh" >&2
    exit 1
  fi
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

# 8001 被占用时顺延，避免「端口已占用」直接起不来。
# 用 Python socket 试探，不依赖 lsof —— macOS 自带 lsof，但很多精简 Linux 镜像没有，
# 用户也不想为启动一个本地服务先装系统工具。
PORT=$("$VENV/bin/python" - "${LAW_PORT:-8001}" <<'PY'
import socket
import sys

for port in range(int(sys.argv[1]), 8011):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            continue
        print(port)
        break
PY
)
if [ -z "$PORT" ]; then
  echo "端口 8001–8010 都被占用了，服务没能启动。" >&2
  echo "  先看是谁占着 8001：" >&2
  echo "    macOS / Linux：lsof -nP -iTCP:8001 -sTCP:LISTEN" >&2
  echo "    Windows：      netstat -ano | findstr :8001" >&2
  echo "  或者换一个空闲端口启动：" >&2
  echo "    LAW_PORT=8020 ./start.sh" >&2
  exit 2
fi
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
