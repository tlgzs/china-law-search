#!/usr/bin/env python3
"""启动前检查 Python 版本。

server/ 的 FastAPI 路由签名用了 PEP 604 联合类型（str | None），
Python 3.9 在 import 阶段就会抛 TypeError。这里提前拦住，给用户「怎么办」，
而不是让他对着一段调用栈自己猜。

本脚本只用标准库，且自身必须能在 3.9 上运行 —— 它是来报错的，不能先崩掉。

用法：
  python3 tools/check_python.py        # 版本够：静默退出 0；不够：打印提示并退出 1
"""
import sys

MIN = (3, 10)
MIN_TEXT = "3.10"


def main() -> int:
    ver = sys.version_info
    if ver[:2] >= MIN:
        return 0

    print(
        f"\n本服务需要 Python {MIN_TEXT} 或更高版本，当前是 "
        f"{ver.major}.{ver.minor}.{ver.micro}。\n"
        "\n"
        "系统自带的 Python 不用动，另装一个独立版本即可（不改系统设置，也不影响系统 Python）：\n"
        "\n"
        "  方式一：uv（推荐，只装到用户目录）\n"
        "    curl -LsSf https://astral.sh/uv/install.sh | sh\n"
        "    uv venv --python 3.12 .venv\n"
        "\n"
        "  方式二：已有 Homebrew\n"
        "    brew install python@3.12\n"
        "    python3.12 -m venv .venv\n"
        "\n"
        "  本机已经装了别的版本（如 python3.12）也可以直接用它：\n"
        "    python3.12 -m venv .venv\n"
        "\n"
        "如果目录里已有 .venv 或 venv，它是用旧版本建的，删掉后重跑：\n"
        "  rm -rf .venv venv\n"
        "\n"
        "装好后重新运行启动脚本即可。详见 INSTALL.md 第 3.1 节。\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
