@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ======================================
echo   壹律法条 · 本地法规检索服务启动中
echo ======================================

where python >nul 2>nul
if errorlevel 1 (
  echo 未检测到 Python。请先安装 Python 3.9+ 并勾选 Add to PATH。
  pause
  exit /b 1
)

rem 虚拟环境目录两种命名都认，与 update.sh 保持一致：优先 .venv（脚本自己建的那个），
rem 其次手建的 venv。不这样的话，手工建过 venv 的机器会再建一个 .venv 白装一遍依赖。
set "VENV=.venv"
if not exist ".venv\Scripts\python.exe" if exist "venv\Scripts\python.exe" set "VENV=venv"
if not exist "%VENV%\Scripts\python.exe" (
  echo 首次运行，正在创建运行环境…
  python -m venv "%VENV%"
)

"%VENV%\Scripts\python" -c "import fastapi, uvicorn" >nul 2>nul
if errorlevel 1 (
  echo 正在安装依赖…
  "%VENV%\Scripts\python" -m pip install -q --upgrade pip
  "%VENV%\Scripts\python" -m pip install -q fastapi "uvicorn[standard]"
)

if not exist data\law_data.db (
  echo.
  echo 提示：未找到数据文件 data\law_data.db
  echo       运行下面这行自动下载（约 676 MB，装过 zstd 则是 514 MB，含校验）：
  echo         python tools\fetch_data.py
  echo       服务仍会启动，但没有数据可查。
  echo.
)

set LAW_HOST=127.0.0.1
if not defined LAW_API_ENFORCE set LAW_API_ENFORCE=0

rem 8001 被占用时顺延，避免「端口已占用」直接起不来
set LAW_PORT=8001
:findport
netstat -ano | findstr /R /C:"TCP.*:%LAW_PORT% .*LISTENING" >nul 2>nul
if errorlevel 1 goto portok
if %LAW_PORT% GEQ 8010 goto portok
set /a LAW_PORT=%LAW_PORT%+1
goto findport
:portok

echo.
echo 本地查询界面：http://127.0.0.1:%LAW_PORT%
echo 关掉这个窗口即停止服务。
echo.

rem 延迟 3 秒再开浏览器，等服务起来
start /min powershell -NoProfile -Command "Start-Sleep 3; Start-Process 'http://127.0.0.1:%LAW_PORT%'"
"%VENV%\Scripts\python" -m uvicorn server.law_search_service:app --host 127.0.0.1 --port %LAW_PORT%
pause
