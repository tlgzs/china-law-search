#!/usr/bin/env python3
# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""把「壹律法条」注册到各家 agent 客户端（MCP 接入）。

用法：
    python3 mcp/install.py --list                 # 看本机装了哪些客户端
    python3 mcp/install.py --client workbuddy     # 预览要写入什么（不落盘）
    python3 mcp/install.py --client workbuddy --yes   # 确认后真正写入
    python3 mcp/install.py --all --yes            # 一次性写全部已探测到的客户端
    python3 mcp/install.py --client workbuddy --yes --remove   # 移除

三条设计原则：

1. **默认不写**。不加 --yes 只打印将要发生的改动。改的是别人的配置文件，
   猜错一次的代价是那个软件起不来。
2. **先备份再写**。每次写入前把原文件复制成 *.bak-时间戳，出问题能一键退回。
3. **只动我们自己的条目**。合并写入，不动别人已经配好的其他 MCP 服务；
   JSON 解析失败时直接中止，绝不"试着修一下"。

一个必须知道的前提：**写文件 ≠ 生效**。
WorkBuddy 要在连接器页点「信任」，Trae / Cursor 要重启，
这些脚本做不到，见 mcp/README.md 的逐步说明。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

SERVER_NAME = "yilv-law"
DISPLAY_NAME = "壹律法条"

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent


# ============================================================
# 客户端登记表
# ============================================================
# entry_style:
#   "plain"  —— {"command", "args", "env"}
#   "typed"  —— 多一个 "type": "stdio"（WorkBuddy / Claude Code 需要）
# root_key: 条目要挂在配置文件的哪个对象下面
# verified: 是否在本机实际验证过（没验证过的不写成"已验证"）

CLIENTS: dict[str, dict] = {
    "workbuddy": {
        "label": "WorkBuddy（腾讯）",
        "kind": "json",
        "root_key": ["mcpServers"],
        "entry_style": "typed",
        "paths": ["~/.workbuddy/mcp.json"],
        "verified": True,
        "post_note": "写入后打开 WorkBuddy → 连接器 → 自定义连接器 → MCP 服务管理，"
                     "找到 yilv-law 点「信任」。不点这一下不会生效。",
    },
    "trae": {
        "label": "Trae（字节）",
        "kind": "json",
        "root_key": ["mcpServers"],
        "entry_style": "plain",
        "paths": [
            "~/Library/Application Support/Trae/User/mcp.json",
            "~/Library/Application Support/Trae/User/settings/mcp.json",
            "~/Library/Application Support/TRAE SOLO CN/User/mcp.json",
            "%APPDATA%/Trae/User/mcp.json",
            "%APPDATA%/Trae/User/settings/mcp.json",
            "%APPDATA%/TRAE SOLO CN/User/mcp.json",
            "~/.config/Trae/User/mcp.json",
        ],
        "verified": False,
        "post_note": "Trae 的用户级配置路径各家说法不一致，脚本只会写「目录已经存在」的那个。"
                     "写完必须完全退出并重启 Trae。若重启后没出现，改用 Trae 设置 → MCP → "
                     "手动配置，把 mcp/configs/trae.json 的内容粘进去。",
    },
    "zcode": {
        "label": "ZCode（智谱）",
        "kind": "json",
        "root_key": ["mcp", "servers"],
        "entry_style": "plain",
        "paths": ["~/.zcode/cli/config.json"],
        "verified": True,
        "post_note": "ZCode 原生配置键是 mcp.servers（不是 mcpServers），脚本已按原生命名写。"
                     "改完新开一个会话才生效。",
    },
    "claude-code": {
        "label": "Claude Code",
        "kind": "json",
        "root_key": ["mcpServers"],
        "entry_style": "typed",
        "paths": ["~/.claude.json"],
        "verified": True,
        "post_note": "写的是 user scope（所有项目可用）。也可以改用官方命令："
                     "claude mcp add yilv-law -- <python> <server.py 绝对路径>",
    },
    "codex": {
        "label": "Codex CLI（OpenAI）",
        "kind": "toml",
        "paths": ["~/.codex/config.toml"],
        "verified": True,
        "post_note": "Codex 用 TOML 不是 JSON，脚本会追加 [mcp_servers.yilv-law] 段。"
                     "追加后重开终端。",
    },
    "cursor": {
        "label": "Cursor",
        "kind": "json",
        "root_key": ["mcpServers"],
        "entry_style": "plain",
        "paths": ["~/.cursor/mcp.json"],
        "verified": False,
        "post_note": "写入后在 Cursor → Settings → MCP 里确认开关为开启，并重启 Cursor。",
    },
    "cline": {
        "label": "Cline（VS Code 扩展）",
        "kind": "json",
        "root_key": ["mcpServers"],
        "entry_style": "plain",
        "paths": [
            "~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json",
            "%APPDATA%/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json",
            "~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json",
        ],
        "verified": False,
        "post_note": "未安装 Cline 扩展时目录不存在，脚本会跳过。",
    },
    "windsurf": {
        "label": "Windsurf",
        "kind": "json",
        "root_key": ["mcpServers"],
        "entry_style": "plain",
        "paths": ["~/.codeium/windsurf/mcp_config.json"],
        "verified": False,
        "post_note": "写入后在 Windsurf 的 Cascade → MCP 面板点 Refresh。",
    },
}


# ============================================================
# 路径与命令
# ============================================================

def _expand(raw: str) -> Path:
    raw = raw.replace("%APPDATA%", os.environ.get("APPDATA", ""))
    return Path(os.path.expandvars(os.path.expanduser(raw)))


def detect_path(client: dict) -> Path | None:
    """返回第一个「父目录已存在」的候选路径。

    只写已存在的目录：凭空创建一个 Trae 的配置目录，Trae 根本不会读它，
    用户却会以为装好了——这比不装更糟。
    """
    for raw in client["paths"]:
        p = _expand(raw)
        if p.exists():
            return p
        if p.parent.is_dir():
            return p
    return None


def detect_all() -> dict[str, Path | None]:
    return {name: detect_path(cfg) for name, cfg in CLIENTS.items()}


def python_command() -> str:
    """用「当前运行本脚本的解释器」的路径。

    这条路很重要：如果用户是在 venv 里跑的，写进去的就是 venv 里的 python，
    agent 拉起子进程时才能 import 到库。写 'python3' 全靠 PATH，GUI 应用
    经常拿不到用户的 PATH，最后表现为「MCP 连上了但工具全报错」。

    这里**故意不 resolve()**：venv/bin/python 通常是指向基础解释器的软链，
    resolve 之后会变成系统 python，venv 里装的包就全丢了。
    """
    return sys.executable or "python3"


def server_script() -> str:
    return str((_HERE / "server.py").resolve())


def build_entry(db_path: str | None, style: str) -> dict:
    entry: dict = {"command": python_command(), "args": [server_script()]}
    if db_path:
        entry["env"] = {"LAW_DB_PATH": str(Path(db_path).expanduser().resolve())}
    if style == "typed":
        entry["type"] = "stdio"
    return entry


def default_db() -> str | None:
    """默认库位置：<仓库根>/data/law_data.db。

    **必须校验大小**：服务端启动时会 sqlite3.connect() 并在文件不存在时创建空库，
    所以「文件存在」不等于「有数据」。0 字节的库会让每条查询都报 no such table，
    却看起来一切正常——这种错最难排查。
    """
    env_p = os.environ.get("LAW_DB_PATH")
    if env_p:
        p = Path(env_p).expanduser()
        if p.is_file() and p.stat().st_size > 0:
            return str(p)
    p = _REPO / "data" / "law_data.db"
    if p.is_file() and p.stat().st_size > 0:
        return str(p)
    return None


# ============================================================
# JSON 客户端
# ============================================================

def _dig(cfg: dict, root_key: list[str], create: bool) -> dict:
    node = cfg
    for key in root_key:
        if key not in node or not isinstance(node[key], dict):
            if not create:
                return {}
            node[key] = {}
        node = node[key]
    return node


def plan_json(path: Path, client: dict, entry: dict, remove: bool) -> tuple[dict | None, str]:
    """返回 (新配置对象 | None, 说明)。None 表示无需改动。"""
    if not path.exists():
        if remove:
            return None, "文件不存在，无需移除"
        cfg = {}
    else:
        try:
            text = path.read_text(encoding="utf-8").strip()
            cfg = json.loads(text) if text else {}
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"{path} 不是合法 JSON（{exc}）。已中止，未做任何修改。"
                "请手动检查该文件的括号与逗号。"
            ) from exc

    servers = _dig(cfg, client["root_key"], create=not remove)
    if remove:
        if SERVER_NAME not in servers:
            return None, "配置里没有 yilv-law，无需移除"
        servers.pop(SERVER_NAME)
        return cfg, f"从 {path} 移除 yilv-law"
    if SERVER_NAME in servers:
        return cfg, f"{path} 里已有 yilv-law，将被覆盖更新"
    return cfg, f"向 {path} 添加 yilv-law"


def write_json(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    backup(path)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# ============================================================
# TOML 客户端（Codex）
# ============================================================

def _strip_toml_block(text: str, prefix: str) -> tuple[str, bool]:
    """删掉 [mcp_servers.NAME] 及其所有子表（如 .env），返回 (剩余文本, 是否找到)。

    必须连子表一起删：只删主表会留下一个孤立的 [mcp_servers.NAME.env]，
    TOML 解析直接报错，整个 Codex 配置就废了。
    """
    out: list[str] = []
    skipping = False
    found = False
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("[") and s.endswith("]"):
            table = s.strip("[]").strip()
            skipping = table == prefix or table.startswith(prefix + ".")
            if skipping:
                found = True
                # 连同紧邻上方的空行与标记注释一起清掉，否则反复装卸会堆一片孤行注释
                if table == prefix:
                    while out and out[-1].strip() == "":
                        out.pop()
                    if out and out[-1].strip().startswith("#") and "壹律法条" in out[-1]:
                        out.pop()
                continue
        if not skipping:
            out.append(ln)
    return "\n".join(out).rstrip() + "\n" if out else "", found


def _toml_block(entry: dict) -> str:
    header = f"[mcp_servers.{SERVER_NAME}]"
    lines = [
        "",
        "# 壹律法条（中国法律法规条文，本地只读）",
        header,
        'type = "stdio"',
        f'command = {json.dumps(entry["command"], ensure_ascii=False)}',
        f'args = [{json.dumps(entry["args"][0], ensure_ascii=False)}]',
    ]
    if entry.get("env"):
        lines.append("")
        lines.append(f"[mcp_servers.{SERVER_NAME}.env]")
        for k, v in entry["env"].items():
            lines.append(f'{k} = {json.dumps(v, ensure_ascii=False)}')
    return "\n".join(lines) + "\n"


def plan_toml(path: Path, entry: dict, remove: bool) -> tuple[str | None, str]:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    prefix = f"mcp_servers.{SERVER_NAME}"
    cleaned, found = _strip_toml_block(text, prefix)

    if remove:
        if not found:
            return None, "配置里没有 yilv-law，无需移除"
        return cleaned, "已移除"

    base = cleaned if found else text
    if base and not base.endswith("\n"):
        base += "\n"
    return base + _toml_block(entry), "将替换已有段落" if found else "将追加新段落"


# ============================================================
# 备份与执行
# ============================================================

def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = path.with_name(f"{path.name}.bak-{ts}")
    shutil.copy2(path, dst)
    return dst


def run_client(name: str, entry: dict, args, results: list[dict]) -> None:
    cfg = CLIENTS[name]
    path = detect_path(cfg)
    record = {"client": name, "label": cfg["label"], "path": str(path) if path else None,
              "status": "", "detail": "", "post_note": cfg.get("post_note", "")}

    if not path:
        record["status"] = "skip"
        record["detail"] = "未检测到该客户端（配置目录不存在）"
        results.append(record)
        return

    try:
        if cfg["kind"] == "json":
            new_cfg, detail = plan_json(path, cfg, entry, args.remove)
            record["detail"] = detail
            if new_cfg is None:
                record["status"] = "noop"
            else:
                if cfg["root_key"]:
                    servers = _dig(new_cfg, cfg["root_key"], create=True)
                    if args.remove:
                        servers.pop(SERVER_NAME, None)
                    else:
                        servers[SERVER_NAME] = entry
                record["status"] = "done" if args.yes else "dry-run"
                if args.yes:
                    write_json(path, new_cfg)
        else:  # toml
            new_text, detail = plan_toml(path, entry, args.remove)
            record["detail"] = detail
            if new_text is None:
                record["status"] = "noop"
            else:
                record["status"] = "done" if args.yes else "dry-run"
                if args.yes:
                    if not path.exists():
                        path.parent.mkdir(parents=True, exist_ok=True)
                    backup(path)
                    path.write_text(new_text, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        record["status"] = "error"
        record["detail"] = str(exc)
    results.append(record)


# ============================================================
# CLI
# ============================================================

def cmd_list(db_path: str | None) -> int:
    detected = detect_all()
    print(f"{'客户端':<26} {'状态':<10} 配置文件")
    print("-" * 96)
    for name, cfg in CLIENTS.items():
        p = detected[name]
        if p is None:
            print(f"{cfg['label']:<24} {'未安装':<10} —")
            continue
        mark = "" if cfg.get("verified") else "  (未实测)"
        exists = "已存在" if p.exists() else "待创建"
        print(f"{cfg['label']:<24} {exists:<10} {p}{mark}")
    print()
    print(f"将使用的解释器：{python_command()}")
    print(f"MCP 服务器脚本：{server_script()}")
    if db_path:
        print(f"数据库：{db_path}")
    else:
        print("数据库：（未找到 data/law_data.db）")
        print("  先下载数据再装，否则 agent 查不到任何条文：")
        print("    python3 tools/fetch_data.py")
    return 0


def cmd_print(name: str, db_path: str | None) -> int:
    """打印该客户端的配置片段（路径已填好，可直接粘贴）。

    片段不另外存成文件：存一份静态模板必然和脚本生成的结果分叉，
    而配置错一个字符连不上，排查成本远高于省下的几行代码。
    """
    cfg = CLIENTS[name]
    entry = build_entry(db_path, cfg.get("entry_style", "plain"))
    print(f"# {cfg['label']} — 写入 {cfg['paths'][0]}")
    if not cfg.get("verified"):
        print("# 注意：这一家未在本项目开发机上实测，写入后请实际验证一次。")
    print()
    if cfg["kind"] == "toml":
        print(_toml_block(entry).strip())
    else:
        wrapper: dict = {}
        node = wrapper
        for key in cfg["root_key"]:
            node[key] = {}
            node = node[key]
        node[SERVER_NAME] = entry
        print(json.dumps(wrapper, ensure_ascii=False, indent=2))
    print()
    if cfg.get("post_note"):
        print(f"# {cfg['post_note']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="把壹律法条注册到 agent 客户端（MCP）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--list", action="store_true", help="列出本机客户端及其配置状态")
    parser.add_argument("--client", action="append", choices=sorted(CLIENTS),
                        help="指定客户端，可重复")
    parser.add_argument("--all", action="store_true", help="处理所有已探测到的客户端")
    parser.add_argument("--yes", action="store_true", help="真正写入（不加则只预览）")
    parser.add_argument("--remove", action="store_true", help="移除 yilv-law 条目")
    parser.add_argument("--print", dest="print_client", choices=sorted(CLIENTS),
                        metavar="CLIENT", help="打印该客户端的配置片段（路径已填好）")
    parser.add_argument("--db", help="law_data.db 路径")
    args = parser.parse_args()

    db_path = args.db or default_db()

    if args.print_client:
        return cmd_print(args.print_client, db_path)
    if args.list or (not args.client and not args.all):
        resolved = str(Path(db_path).expanduser().resolve()) if db_path else None
        return cmd_list(resolved)

    targets = list(args.client or [])
    if args.all:
        detected = detect_all()
        targets = [n for n, p in detected.items() if p is not None]

    if not targets:
        print("没有可处理的客户端。用 --list 看看本机装了什么。", file=sys.stderr)
        return 1

    results: list[dict] = []
    for name in targets:
        style = CLIENTS[name].get("entry_style", "plain")
        run_client(name, build_entry(db_path, style), args, results)

    print()
    for r in results:
        icon = {"done": "✔", "dry-run": "·", "noop": "=", "skip": "-", "error": "✘"}[r["status"]]
        print(f"{icon} {r['label']}")
        if r["path"]:
            print(f"    {r['path']}")
        if r["detail"]:
            print(f"    {r['detail']}")
        if r["status"] in ("done", "dry-run") and r["post_note"]:
            print(f"    → {r['post_note']}")

    if not args.yes:
        print()
        print("以上为预览，未修改任何文件。确认无误后加 --yes 再执行一次。")
    else:
        print()
        print("已写入。原文件已备份为同目录下的 *.bak-时间戳。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
