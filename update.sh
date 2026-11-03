#!/bin/bash
# 命令行更新：./update.sh
#
# 只做两件事：查到有新数据就下载安装，装完打印版本。程序本身有新版本时只提示，
# 不会自动改代码 —— 用户本地可能还有未提交的改动，自动 git pull 会把它们冲掉。
#
# 想只看不装：python3 tools/check_update.py
# 想看完整版本对比：python3 tools/check_update.py --json
set -u
cd "$(dirname "$0")" || exit 1

# 虚拟环境目录两种命名都认：仓库脚本历史上有 .venv 和 venv 两套
PY=""
for c in .venv venv; do
  if [ -x "$c/bin/python" ]; then PY="$c/bin/python"; break; fi
done
if [ -z "$PY" ]; then
  if command -v python3 >/dev/null 2>&1; then
    PY=python3
  else
    echo "未检测到 python3，请先安装 Python 3.9 以上版本。" >&2
    exit 1
  fi
fi

"$PY" tools/check_update.py --update
code=$?

echo ""
if [ $code -eq 0 ]; then
  echo "完成。库是同一个文件，更新后刷新网页即可；只有程序版本更新了才需要重启服务。"
else
  echo "更新未完成（退出码 $code）。数据没有被动过，可以继续正常使用。"
  echo "  也可以在网页顶部点「立即更新」，或手动下载："
  echo "    python3 tools/fetch_data.py"
fi
exit $code
