#!/bin/bash
# 双击启动：自动准备环境 → 启动本地法规检索服务 → 打开浏览器
cd "$(dirname "$0")" || exit 1

echo "======================================"
echo "  壹律法条 · 本地法规检索服务启动中"
echo "======================================"

if ! command -v python3 >/dev/null 2>&1; then
  echo "未检测到 python3。请先安装 Python 3.10+（https://www.python.org/downloads/）后重试。"
  read -r -p "按回车键关闭…" _
  exit 1
fi

if [ ! -d .venv ]; then
  echo "首次运行，正在创建运行环境（约 1 分钟）…"
  python3 -m venv .venv || { echo "创建虚拟环境失败"; read -r -p "按回车键关闭…" _; exit 1; }
fi

if ! .venv/bin/python -c "import fastapi, uvicorn" >/dev/null 2>&1; then
  echo "正在安装依赖…"
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q fastapi "uvicorn[standard]" || { echo "依赖安装失败，请检查网络"; read -r -p "按回车键关闭…" _; exit 1; }
fi

if [ ! -f data/law_data.db ]; then
  echo ""
  echo "提示：未找到数据文件 data/law_data.db"
  echo "     运行下面这行自动下载（约 262 MB，含校验）："
  echo "       python3 tools/fetch_data.py"
  echo "     服务仍会启动，但没有数据可查。"
  echo ""
fi

PORT=8001
while lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 && [ $PORT -lt 8010 ]; do
  PORT=$((PORT + 1))
done

echo "服务地址：http://127.0.0.1:$PORT"
(
  sleep 2
  open "http://127.0.0.1:$PORT" 2>/dev/null
) &

export LAW_HOST=127.0.0.1
export LAW_PORT=$PORT
export LAW_API_ENFORCE=${LAW_API_ENFORCE:-0}
exec .venv/bin/python -m uvicorn server.law_search_service:app --host 127.0.0.1 --port $PORT
