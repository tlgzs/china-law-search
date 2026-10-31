#!/usr/bin/env python3
"""壹律法条 · MCP 服务器（stdio）。

设计取舍：
  - **只依赖 Python 标准库**。不用官方 mcp SDK：律师机器上多一个依赖就多一个
    装不上的理由，而 stdio 传输本质就是「一行一个 JSON-RPC」，手写更可控。
  - **直连 SQLite，不走 HTTP**。agent 自己拉起子进程、用完即退，
    不需要用户先「启动服务」，也就没有端口占用和常驻进程。
  - **只读**。这里没有任何写库的路径，工具全部是查询。
  - 查询逻辑复用 server/law_query_core.py，与网页版同一套规则。

四个工具，少而精——工具多了 agent 反而挑不准：
  get_article      法名 + 条号 → 逐字原文（含「之一」增补条）
  get_law          法名 → 章节目录 + 版本沿革 + 条文
  search_articles  多关键词 → 候选条文（精确子串，不做语义理解）
  verify_citation  一段引文 → 是否逐字命中、命中版本是否现行

自检：python3 mcp/server.py --selftest
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import traceback
from pathlib import Path

SERVER_NAME = "yilv-law"
SERVER_VERSION = "1.0.0"

# 支持的协议版本（新 → 旧）。客户端请求哪个就回哪个，不认识的回最老的那个。
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
FALLBACK_PROTOCOL = "2024-11-05"

# ------------------------------------------------------------
# 定位查询内核（mcp/ 与 server/ 是平级目录）
# ------------------------------------------------------------

_HERE = Path(__file__).resolve().parent
for _cand in (_HERE.parent / "server", _HERE.parent, _HERE):
    if (_cand / "law_query_core.py").exists():
        sys.path.insert(0, str(_cand))
        break

try:
    from law_query_core import (  # noqa: E402
        LawStore,
        parse_article_number,
        parse_article_suffix,
    )
except ImportError as exc:  # pragma: no cover - 只会在部署残缺时触发
    sys.stderr.write(
        f"[{SERVER_NAME}] 找不到 server/law_query_core.py，MCP 无法启动：{exc}\n"
        "请确认整个仓库目录完整（server/ 与 mcp/ 是平级目录）。\n"
    )
    raise SystemExit(1) from exc


# ------------------------------------------------------------
# 数据库定位
# ------------------------------------------------------------

def resolve_db_path(cli_path: str | None = None) -> str | None:
    """按优先级定位库文件，找不到返回 None（不抛异常，让服务器仍能初始化）。

    顺序：
      1. --db 命令行参数
      2. 环境变量 LAW_DB_PATH
      3. <仓库根>/data/law_data.db          （源码运行）
      4. 可执行文件同级 / 上级 Resources     （dmg / exe 打包形态）
    """
    candidates: list[Path] = []
    if cli_path:
        candidates.append(Path(cli_path).expanduser())
    env_path = os.environ.get("LAW_DB_PATH")
    if env_path:
        candidates.append(Path(env_path).expanduser())

    root = _HERE.parent
    candidates.append(root / "data" / "law_data.db")

    exe_dir = Path(sys.executable).resolve().parent
    candidates.extend([
        exe_dir / "law_data.db",
        exe_dir / ".." / "Resources" / "law_data.db",
        exe_dir / ".." / "data" / "law_data.db",
    ])

    for p in candidates:
        try:
            if p.is_file() and p.stat().st_size > 0:
                return str(p.resolve())
        except OSError:
            continue
    return None


# ------------------------------------------------------------
# 工具定义
# ------------------------------------------------------------

TOOLS = [
    {
        "name": "get_article",
        "description": (
            "按「法律名称 + 条号」取一条法条的逐字原文。这是最精确的取法条方式，"
            "适合已经知道出处（例如「劳动合同法第八十二条」）时使用。\n"
            "支持增补条：条号写「第一百三十三条之一」或分别传 article_number=133、"
            "article_suffix=之一，两者不会串号。\n"
            "返回的 status 为 effective 表示现行有效，repealed 表示已被修订"
            "（此时 repeal_date / replaced_by 会给出失效日期和替代法规）。\n"
            "citation_hint 字段可直接复制为规范引用格式。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "law_name": {
                    "type": "string",
                    "description": "法律名称，可省略书名号和「中华人民共和国」前缀，例如「劳动合同法」。",
                },
                "article_number": {
                    "type": ["string", "integer"],
                    "description": "条号，支持「第八十二条」「82」「第一百三十三条之一」等写法。",
                },
                "article_suffix": {
                    "type": "string",
                    "description": "增补条后缀，如「之一」。条号里已含后缀时可省略。",
                },
                "include_history": {
                    "type": "boolean",
                    "description": "为 true 时允许返回已被修订的旧条文。默认 false，只取现行。",
                    "default": False,
                },
            },
            "required": ["law_name", "article_number"],
        },
    },
    {
        "name": "get_law",
        "description": (
            "按法律名称取整部法规：章节目录、版本沿革、全部条文。\n"
            "适合需要通读、或不确定条号时先看目录再用 get_article 精确定位。\n"
            "条文较多时建议 with_content=false 先取目录，再按需取全文。\n"
            "name_candidates 非空表示法名有歧义，多个候选里第一个是最短匹配（通常是正主）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "law_name": {"type": "string", "description": "法律名称，可模糊，如「劳动合同法」。"},
                "with_content": {
                    "type": "boolean",
                    "description": "是否返回条文正文。false 时只返回目录与元信息。",
                    "default": True,
                },
                "max_articles": {
                    "type": "integer",
                    "description": "最多返回多少条正文（按条号升序）。0 表示全部。",
                    "default": 0,
                },
                "include_history": {
                    "type": "boolean",
                    "description": "是否包含已被修订的旧条文。默认 false。",
                    "default": False,
                },
            },
            "required": ["law_name"],
        },
    },
    {
        "name": "search_articles",
        "description": (
            "按关键词在条文正文里做精确子串检索，返回候选条文。\n"
            "**这不是语义检索**：它按字面匹配，不理解同义词。'欠薪' 与 '拖欠劳动报酬' "
            "在本工具眼里是两回事——要覆盖同义说法，请把多个说法一起放进 keywords 并用 "
            "mode=OR。\n"
            "因此更推荐的做法是：先由你（agent）把用户的口语表达拆成法律术语关键词，"
            "再用本工具查证。\n"
            "返回按相关性排序，每条带 40 字摘要；需要全文时用 get_article 按条号再取。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "关键词列表，例如 [\"拖欠劳动报酬\", \"工资\"]。建议 2-5 个。",
                },
                "mode": {
                    "type": "string",
                    "enum": ["AND", "OR"],
                    "description": "AND 要求全部命中（更准），OR 命中任一（更全）。默认 AND。",
                    "default": "AND",
                },
                "k": {
                    "type": "integer",
                    "description": "返回条数上限，默认 8。",
                    "default": 8,
                },
                "law_type": {
                    "type": "string",
                    "description": "限定法规类别，可选值见 stats 的 by_law_type 键。",
                },
                "include_history": {
                    "type": "boolean",
                    "description": "是否包含已失效条文。默认 false。",
                    "default": False,
                },
            },
            "required": ["keywords"],
        },
    },
    {
        "name": "verify_citation",
        "description": (
            "核验一批法条引用：是否存在、是否现行有效、是否有多个版本、内容与库里是否一致。\n"
            "**在用任何法条支撑结论之前，先跑这一步**——这是本服务器最有价值的工具，"
            "专门用来防止编造条号或引用已废止条文。\n"
            "引用写成「《劳动合同法》第八十二条」或对象 {law_name, article_number} 都可以。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "citations": {
                    "type": "array",
                    "description": "待核验的引用列表，元素为字符串或 {law_name, article_number} 对象。",
                    "items": {
                        "type": ["string", "object"],
                        "properties": {
                            "law_name": {"type": "string"},
                            "article_number": {"type": "string"},
                            "article_suffix": {"type": "string"},
                        },
                    },
                },
                "include_history": {
                    "type": "boolean",
                    "description": "是否用旧版本条文参与核验。默认 false。",
                    "default": False,
                },
            },
            "required": ["citations"],
        },
    },
]


def _ok(payload: dict) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "isError": False,
    }


def _err(message: str, **extra) -> dict:
    payload = {"error": message}
    payload.update(extra)
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "isError": True,
    }


# ------------------------------------------------------------
# 服务器
# ------------------------------------------------------------

class LawMcpServer:
    def __init__(self, db_path: str | None, default_limit: int = 8):
        self.db_path = db_path
        self.default_limit = default_limit
        self._conn: sqlite3.Connection | None = None
        self._store: LawStore | None = None
        self._snapshot: dict | None = None

    # ---- 库 ----

    def store(self) -> LawStore:
        if self._store is not None:
            return self._store
        if not self.db_path:
            raise FileNotFoundError(
                "未找到法条数据库。请把 law_data.db 放到 <仓库根>/data/ 下，"
                "或用环境变量 LAW_DB_PATH 指定路径。"
            )
        # 只读模式打开：MCP 侧绝不写库，也避免和其他进程抢写锁
        uri = f"file:{self.db_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        self._conn = conn
        self._store = LawStore(conn)
        return self._store

    def snapshot(self) -> dict:
        """数据快照信息。每条响应都带上，明确「这是某天的库」而非实时同步。"""
        if self._snapshot is not None:
            return self._snapshot
        info: dict = {"db_path": self.db_path or "", "corpus_version": "", "generated_at": "",
                      "data_cutoff": ""}
        try:
            rows = self.store().conn.execute("SELECT key, value FROM corpus_meta").fetchall()
            for r in rows:
                # data_cutoff 是「条文收录到哪一天」，corpus_version 只是「哪天打的包」。
                # agent 回答时效性问题时需要前一个。
                if r["key"] in ("corpus_version", "generated_at", "built_at", "source",
                                "data_cutoff"):
                    info[r["key"]] = r["value"]
        except Exception:  # noqa: BLE001
            pass
        info["notice"] = (
            "库内条文为构建时快照，非实时同步。正式引用前请以国家法律法规数据库"
            "（flk.npc.gov.cn）公布文本为准。"
        )
        self._snapshot = info
        return info

    # ---- 工具实现 ----

    def tool_get_article(self, args: dict) -> dict:
        law_name = str(args.get("law_name") or "").strip()
        raw = args.get("article_number")
        if not law_name:
            return _err("缺少参数 law_name")
        if raw is None or str(raw).strip() == "":
            return _err("缺少参数 article_number")

        num = parse_article_number(str(raw))
        if num <= 0:
            return _err(f"无法解析条号：{raw!r}", hint="可用「第八十二条」或 82。")

        suffix = str(args.get("article_suffix") or "").strip() or parse_article_suffix(str(raw))
        include_history = bool(args.get("include_history", False))

        item = self.store().get_article(law_name, num, suffix, include_history=include_history)
        if not item:
            suggestions = self.store().list_laws(law_name, k=5)
            return _err(
                "未找到该条文",
                law_name=law_name,
                article_number=f"第{num}条{suffix}",
                hint="法名可能有出入，或该条号不存在。可先用 get_law 看目录。",
                law_name_candidates=[s["law_name"] for s in suggestions],
            )

        item["citation_hint"] = f"《{item['law_name']}》{item['article_number']}"
        return _ok({"article": item, "data_snapshot": self.snapshot()})

    def tool_get_law(self, args: dict) -> dict:
        law_name = str(args.get("law_name") or "").strip()
        if not law_name:
            return _err("缺少参数 law_name")
        with_content = bool(args.get("with_content", True))
        max_articles = int(args.get("max_articles") or 0)
        include_history = bool(args.get("include_history", False))

        law = self.store().get_law(
            law_name,
            include_history=include_history,
            with_content=with_content,
            max_articles=max_articles,
        )
        if not law:
            suggestions = self.store().list_laws(law_name, k=8)
            return _err(
                "未找到该法规",
                law_name=law_name,
                hint="试试更短的名称（去掉「中华人民共和国」前缀），或先看候选列表。",
                law_name_candidates=[s["law_name"] for s in suggestions],
            )

        if not with_content:
            law.pop("articles", None)
        return _ok({"law": law, "data_snapshot": self.snapshot()})

    def tool_search_articles(self, args: dict) -> dict:
        raw = args.get("keywords")
        if isinstance(raw, str):
            keywords = [raw]
        elif isinstance(raw, list):
            keywords = [str(x) for x in raw if str(x).strip()]
        else:
            keywords = []
        if not keywords:
            return _err("缺少参数 keywords（字符串数组）")

        k = int(args.get("k") or self.default_limit)
        mode = str(args.get("mode") or "AND").upper()
        law_type = args.get("law_type")
        include_history = bool(args.get("include_history", False))

        store = self.store()
        items = store.search_multi(
            keywords, mode=mode, k=k, law_type=law_type, include_history=include_history
        )
        # AND 无结果时用 OR 再兜一次，并明确告知放宽了条件——比让 agent 自己重试更省调用
        relaxed = False
        if not items and mode == "AND" and len(keywords) > 1:
            items = store.search_multi(
                keywords, mode="OR", k=k, law_type=law_type, include_history=include_history
            )
            relaxed = bool(items)

        return _ok({
            "keywords": keywords,
            "mode": mode,
            "relaxed_to_or": relaxed,
            "count": len(items),
            "results": [
                {
                    "law_name": it["law_name"],
                    "article_number": it["article_number"],
                    "article_suffix": it["article_suffix"],
                    "chapter": it["chapter"],
                    "status": it["status"],
                    "excerpt": (it["content"] or "")[:120],
                }
                for it in items
            ],
            "next_step": "需要全文时用 get_article 传入 law_name 与 article_number。",
            "data_snapshot": self.snapshot(),
        })

    def tool_verify_citation(self, args: dict) -> dict:
        citations = args.get("citations")
        if not isinstance(citations, list) or not citations:
            return _err("缺少参数 citations（引用数组）")
        include_history = bool(args.get("include_history", False))

        result = self.store().verify(citations, include_history=include_history)
        for r in result["results"]:
            if r.get("verified"):
                r["citation_hint"] = f"《{r['law_name']}》{r['article_number']}"
        result["data_snapshot"] = self.snapshot()
        return _ok(result)

    # ---- JSON-RPC 分发 ----

    def dispatch(self, method: str, params: dict) -> dict | None:
        if method == "initialize":
            requested = (params or {}).get("protocolVersion")
            version = requested if requested in SUPPORTED_PROTOCOLS else FALLBACK_PROTOCOL
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "中国法律法规条文接口（离线快照）。所有工具只读。"
                    "取条文用 get_article；不确定条号先 get_law 看目录；"
                    "用条文支撑结论前先跑 verify_citation 核验存在性与时效性。"
                ),
            }

        if method in ("notifications/initialized", "notifications/cancelled"):
            return None

        if method == "ping":
            return {}

        if method == "tools/list":
            return {"tools": TOOLS}

        if method == "tools/call":
            name = (params or {}).get("name")
            arguments = (params or {}).get("arguments") or {}
            handlers = {
                "get_article": self.tool_get_article,
                "get_law": self.tool_get_law,
                "search_articles": self.tool_search_articles,
                "verify_citation": self.tool_verify_citation,
            }
            handler = handlers.get(name)
            if not handler:
                raise ValueError(f"未知工具：{name}")
            try:
                return handler(arguments)
            except FileNotFoundError as exc:
                return _err(str(exc), hint="这是安装问题，不是查询问题。")
            except sqlite3.Error as exc:
                return _err(f"数据库读取失败：{exc}")

        # 部分客户端会探测这些能力，返回空集合比直接报错更友好
        if method == "resources/list":
            return {"resources": []}
        if method == "resources/templates/list":
            return {"resourceTemplates": []}
        if method == "prompts/list":
            return {"prompts": []}

        raise NotImplementedError(f"不支持的方法：{method}")

    def handle_line(self, line: str) -> dict | None:
        line = line.strip()
        if not line:
            return None
        try:
            msg = json.loads(line)
        except json.JSONDecodeError as exc:
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"解析失败：{exc}"},
            }

        msg_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}

        try:
            result = self.dispatch(method, params)
        except NotImplementedError as exc:
            if msg_id is None:
                return None
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32601, "message": str(exc)}}
        except Exception as exc:  # noqa: BLE001
            if msg_id is None:
                return None
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}",
                              "data": traceback.format_exc(limit=3)}}

        if result is None:  # 通知类消息不回包
            return None
        if msg_id is None:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}


# ------------------------------------------------------------
# 自检
# ------------------------------------------------------------

def selftest(db_path: str | None) -> int:
    resolved = resolve_db_path(db_path)
    print(f"库路径：{resolved or '（未找到）'}")
    if not resolved:
        print("FAIL 找不到库文件")
        return 1

    srv = LawMcpServer(resolved)
    ok = True

    init = srv.handle_line(json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "selftest", "version": "0"}},
    }))
    assert init and init["result"]["serverInfo"]["name"] == SERVER_NAME
    print(f"OK   initialize → protocol={init['result']['protocolVersion']}")

    tools = srv.handle_line(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
    names = [t["name"] for t in tools["result"]["tools"]]
    print(f"OK   tools/list → {names}")

    stats = srv.store().stats()
    print(f"OK   stats → 条文 {stats['articles_total']} / 现行 {stats['articles_effective']}"
          f" / 法规 {stats['laws']}")
    print(f"     快照 → corpus_version={srv.snapshot().get('corpus_version')!r}"
          f" 条文截至={srv.snapshot().get('data_cutoff') or '未标注'}")

    sample = srv.store().conn.execute(
        "SELECT law_name, article_number, article_number_num FROM law_articles "
        "WHERE status IS NULL OR status != 'repealed' LIMIT 1"
    ).fetchone()
    if sample:
        r = srv.handle_line(json.dumps({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "get_article", "arguments": {
                "law_name": sample["law_name"], "article_number": sample["article_number"]}},
        }))
        text = r["result"]["content"][0]["text"]
        ok = ok and not r["result"]["isError"]
        print(f"OK   get_article → {sample['law_name']} {sample['article_number']} "
              f"({len(text)} 字节)")

        v = srv.handle_line(json.dumps({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "verify_citation", "arguments": {"citations": [
                f"《{sample['law_name']}》{sample['article_number']}",
                "《不存在的法》第一条",
            ]}},
        }))
        vd = json.loads(v["result"]["content"][0]["text"])
        print(f"OK   verify_citation → {vd['verified_count']}/{vd['total']} 命中"
              f"（预期 1/2）")
        ok = ok and vd["verified_count"] == 1

    print("\n全部通过" if ok else "\n有失败项")
    return 0 if ok else 1


# ------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="壹律法条 MCP 服务器（stdio）")
    parser.add_argument("--db", help="law_data.db 路径（默认自动查找）")
    parser.add_argument("--selftest", action="store_true", help="自检后退出，不进入 stdio 循环")
    parser.add_argument("--limit", type=int, default=8, help="search_articles 默认返回条数")
    args = parser.parse_args()

    if args.selftest:
        return selftest(args.db)

    server = LawMcpServer(resolve_db_path(args.db), default_limit=args.limit)

    # stdout 只用于协议报文，任何日志都走 stderr，否则会污染 JSON-RPC 流
    sys.stderr.write(f"[{SERVER_NAME}] started, db={server.db_path or 'NOT FOUND'}\n")
    sys.stderr.flush()

    for line in sys.stdin:
        try:
            response = server.handle_line(line)
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"[{SERVER_NAME}] handler crashed: {exc}\n")
            continue
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
