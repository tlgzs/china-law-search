@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ======================================
echo   中国法律法规检索 · 本地服务启动中
echo ======================================

where python >nul 2>nul
if errorlevel 1 (
  echo 未检测到 Python。请先安装 Python 3.10+ 并勾选 Add to PATH。
  pause
  exit /b 1
)

if not exist .venv (
  echo 首次运行，正在创建运行环境…
  python -m venv .venv
)

.venv\Scripts\python -c "import fastapi, uvicorn" >nul 2>nul
if errorlevel 1 (
  echo 正在安装依赖…
  .venv\Scripts\python -m pip install -q --upgrade pip
  .venv\Scripts\python -m pip install -q fastapi "uvicorn[standard]"
)

if not exist data\law_data.db (
  echo.
  echo 提示：未找到 data\law_data.db
  echo       请先从 GitHub Releases 下载数据包，解压后把 law_data.db 放进 data 目录。
  echo.
)

set LAW_HOST=127.0.0.1
set LAW_PORT=8001
if not defined LAW_API_ENFORCE set LAW_API_ENFORCE=0

start "" http://127.0.0.1:8001
.venv\Scripts\python -m uvicorn server.law_search_service:app --host 127.0.0.1 --port 8001
pause
