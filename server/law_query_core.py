# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""法条查询内核 — 纯 sqlite3，不依赖 FastAPI / uvicorn。

被两处共用，是唯一实现：
  - server/law_search_service.py   HTTP 接口（网页界面与远程调用）
  - mcp/server.py                  MCP stdio 服务器（agent 调用）

分成一个内核而不各写一份，是因为「条号之一」「法规名归一」这两条规则
一旦在两边走岔，就会出现「网页查得到、agent 查不到」的分裂，
而这种错在 agent 场景下不会有人复核，代价比网页高得多。

设计约束：
  - 只 import 标准库（re / sqlite3 / math），保证 MCP 形态零第三方依赖
  - 所有方法接受外部传入的 sqlite3.Connection，不自建连接、不持全局状态
  - 返回的 dict 结构与原 HTTP 接口逐字段一致，便于两端共用前端与测试
"""

from __future__ import annotations

import datetime
import math
import re
import sqlite3


def _today() -> str:
    return datetime.date.today().isoformat()


def dedupe_versions(items: list[dict]) -> list[dict]:
    """同一条文只留一版：现行 > 已公布未生效 > 历史版本。

    新版公布后、施行前，库里同一条号会同时存在两版正文。检索若原样返回，
    同一条文会出现两次、文字还不同；k 只有 10 条时更会挤掉真正相关的结果。
    保留首次出现的名次，不打乱相关性顺序。
    """
    def rank(it: dict) -> int:
        if it.get("is_repealed"):
            return 2
        return 1 if it.get("is_pending") else 0

    def key(it: dict) -> tuple:
        return (it.get("law_name") or "", it.get("article_number") or "",
                it.get("article_suffix") or "")

    best: dict[tuple, int] = {}
    for it in items:
        k, r = key(it), rank(it)
        if k not in best or r < best[k]:
            best[k] = r

    out: list[dict] = []
    kept: set[tuple] = set()
    for it in items:
        k = key(it)
        if k in kept or best[k] != rank(it):
            continue
        kept.add(k)
        out.append(it)
    return out


def status_rank(alias: str = "") -> str:
    """同一条文取哪一版：现行 > 已公布未生效 > 已废止。

    一部法在库里可以同时躺着两版：新版已公布未生效（pending）、旧版仍在
    施行（effective）。只按 id 倒序取，拿到的是明年才生效的那一版 —— 律师
    今天写意见书就会引来还没施行的条文。所以排序必须先看状态，再看 id。
    """
    p = f"{alias}." if alias else ""
    return (f"CASE WHEN {p}status IS NULL OR {p}status='effective' THEN 0 "
            f"WHEN {p}status='pending' THEN 1 ELSE 2 END")


# ============================================================
# 中文数字与条号解析
# ============================================================

_CN_DIGIT_MAP = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
                 "六": 6, "七": 7, "八": 8, "九": 9}
# 「亿」必须收进来：不收的话「第八十五亿条」会被解析成第八十五条，
# 引文核验就会把一个不存在的条号判成「通过」——这类假阳性比查不到危险得多。
_CN_UNIT_MAP = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}


def chinese_to_arabic(text: str) -> int:
    result = 0
    temp = 0
    for char in text:
        if char in _CN_DIGIT_MAP:
            temp = _CN_DIGIT_MAP[char]
        elif char in _CN_UNIT_MAP:
            if temp == 0:
                temp = 1
            result += temp * _CN_UNIT_MAP[char]
            temp = 0
    return result + temp


def parse_article_number(text: str) -> int:
    """从条号文本中解析阿拉伯数字，支持中文和阿拉伯数字。"""
    text = (text or "").strip()
    if text.isdigit():
        return int(text)
    match = re.search(r"第?([\u4e00-\u9fa5]+)条?", text)
    if match:
        # 解析不出数（如「第82条」里组到的是「第」）时不能直接返回 0，
        # 要让它继续往阿拉伯数字那条路走，否则「第82条」会被判成条号 0。
        value = chinese_to_arabic(match.group(1))
        if value:
            return value
    match = re.search(r"\d+", text)
    if match:
        return int(match.group(0))
    return 0


_ARTICLE_SUFFIX_RE = re.compile(r"第[\u4e00-\u9fa5\d]+条\s*(之[\u4e00-\u9fa5]{1,3})")


def parse_article_suffix(text: str) -> str:
    """提取「之一/之二」增补条后缀，无则返回空串。

    刑法修正案类条文写作「第一百三十三条之一」，与「第一百三十三条」
    是两个独立条文，必须靠后缀区分，否则会串条。
    """
    m = _ARTICLE_SUFFIX_RE.search(text or "")
    return m.group(1) if m else ""


def normalize_law_name(name: str) -> str:
    """标准化法律名称，去除书名号与首尾空白。"""
    return (name or "").strip().replace("《", "").replace("》", "")


def parse_citation_string(citation: str) -> dict:
    """解析字符串形式的法条引用。

    支持：《劳动合同法》第八十二条 ｜ 劳动合同法第82条 ｜ 刑法第一百三十三条之一
    """
    text = (citation or "").strip()
    suffix = parse_article_suffix(text)

    m = re.match(r"[《]?(.+?)[》]?\s*第([\u4e00-\u9fa5]+|\d+)条", text)
    if m:
        num_text = m.group(2)
        num = int(num_text) if num_text.isdigit() else chinese_to_arabic(num_text)
        return {
            "law_name": m.group(1).strip(),
            "article_number": f"第{num_text}条{suffix}",
            "article_num": num,
            "article_suffix": suffix,
        }

    # 兜底：只给数字，例如 "劳动合同法 82"
    num_match = re.search(r"(\d+)", text)
    if num_match:
        return {
            "law_name": text.split(num_match.group(1))[0]
                        .replace("《", "").replace("》", "").replace("第", "").strip(),
            "article_number": f"第{num_match.group(1)}条{suffix}",
            "article_num": int(num_match.group(1)),
            "article_suffix": suffix,
        }

    return {"law_name": "", "article_number": "", "article_num": 0, "article_suffix": ""}


# 「第八十二条」「第八十二、八十七条」「第一百三十三条之一」都认
_CN_NUM_CHARS = r"0-9零一二三四五六七八九十百千万亿两"
_ARTICLE_HIT_RE = re.compile(
    rf"第([{_CN_NUM_CHARS}]+(?:[、,，][{_CN_NUM_CHARS}]+)*)条(之[{_CN_NUM_CHARS}]+)?")

# Python 3.6 兼容写法，避免 f-string 里再嵌 f-string


def _normalize_multi_article(text: str) -> str:
    """把「第八十二、八十七条」补成「第八十二条、第八十七条」。

    中文文书里省略后一个「第」是常见写法，但正则只认带「第…条」的形式，
    不补齐就会被判成解析失败。
    """
    def repl(m):
        suffix = m.group(2) or ""
        parts = [p for p in re.split(r"[、,，]", m.group(1)) if p.strip()]
        return "、".join(f"第{p}条{suffix}" for p in parts)
    return _ARTICLE_HIT_RE.sub(repl, text or "")


def expand_citation(citation: str) -> list:
    """把并列引文展开成多条。

    「《劳动合同法》第八十二条、第八十七条」是一次引两个条文，必须分别核。
    以前只在网页前端拆，直接调 HTTP / MCP 的 agent 传并列引文时，
    后端只解析第一条，第二条被静默丢掉——结果照样报「通过」。
    这是假阳性，比查不到更危险，所以拆在解析层，前端后端共用一套。
    """
    text = _normalize_multi_article((citation or "").strip())
    first = parse_citation_string(text)
    law_name = (first.get("law_name") or "").strip()
    if not law_name:
        return [first]

    hits = list(_ARTICLE_HIT_RE.finditer(text))
    if not hits:
        return [first]

    out = []
    for hit in hits:
        suffix = hit.group(2) or ""
        for num_text in re.split(r"[、,，]", hit.group(1)):
            num_text = num_text.strip()
            if not num_text:
                continue
            num = int(num_text) if num_text.isdigit() else chinese_to_arabic(num_text)
            if num <= 0:
                continue
            out.append({
                "law_name": law_name,
                "article_number": f"第{num_text}条{suffix}",
                "article_num": num,
                "article_suffix": suffix,
            })
    return out if len(out) > 1 else [first]


# ============================================================
# 行 → 字典
# ============================================================

def row_to_item(r, score: float = 0.0) -> dict:
    """把一行法条记录转成统一返回结构（含时效状态，供前端标注旧版本）。"""
    status = r["status"] or "effective"
    effective_date = r["effective_date"] or ""
    # 兜底：库里标 pending 但施行日已过，按现行有效返回。
    # 正常情况 ingest/schema.py 的 refresh_pending_status 已经校正过，
    # 这里再算一遍是防「用 sqlite 直接改库、没走 ensure_schema」的情况。
    if status == "pending" and effective_date and effective_date <= _today():
        status = "effective"
    return {
        "law_name": r["law_name"],
        "law_type": r["law_type"],
        "law_type_label": r["law_type_label"],
        "article_number": r["article_number"],
        "article_num": r["article_number_num"],
        "article_suffix": r["article_suffix"] or "",
        "chapter": r["chapter"],
        "section": r["section"],
        "content": r["content"],
        "publish_date": r["publish_date"] or "",
        "effective_date": effective_date,
        "status": status,
        # 已公布未生效：条文全文可查，但不能当现行依据引用
        "is_pending": status == "pending",
        "is_repealed": status == "repealed",
        "repeal_date": r["repeal_date"] or "",
        "replaced_by": r["replaced_by"] or "",
        "score": score,
    }


_COLS = """a.law_name, a.law_type, a.law_type_label, a.article_number,
           a.article_number_num, a.article_suffix, a.chapter, a.section, a.content,
           a.publish_date, a.effective_date, a.status, a.repeal_date, a.replaced_by"""


def fts_query(q: str) -> str | None:
    """把用户输入转成 FTS5 trigram 查询串。

    trigram 分词器按 3 字滑动窗口切词，短于 3 字的查询无法命中，
    且未加引号时特殊符号会被当成语法 → 统一加双引号并转义。
    """
    cleaned = (q or "").strip().replace('"', '""')
    if len(cleaned) < 3:
        return None
    return f'"{cleaned}"'


def match_terms(q: str, size: int = 2, limit: int = 8) -> list[str]:
    """把查询切成 2 字滑动片段，用于「命中多数即召回」的模糊匹配。

    「未签书面劳动合同」→ 未签/签书/书面/劳动/合同
    中文没有空格、也没有分词，纯字面匹配会漏掉「未与劳动者订立书面劳动合同」这类改写。
    """
    s = re.sub(r"\s+", "", q or "")
    if len(s) < size * 2:
        return []
    grams: list[str] = []
    for i in range(0, len(s) - size + 1):
        g = s[i:i + size]
        if g not in grams:
            grams.append(g)
    return grams[:limit]


def _build_verify_result(
    law_name: str,
    article_number: str,
    verified: bool,
    article_suffix: str = "",
    actual_content: str | None = None,
    error: str | None = None,
    status: str | None = None,
    effective_date: str | None = None,
    repeal_date: str | None = None,
    repeal_reason: str | None = None,
    replaced_by: str | None = None,
    is_principle_clause: bool = False,
    has_multiple_versions: bool = False,
    source_citation: str = "",
) -> dict:
    """构建校验结果，确保所有字段都返回。"""
    pending = status == "pending"
    return {
        "law_name": law_name,
        "article_number": article_number,
        "article_suffix": article_suffix,
        "verified": verified,
        "actual_content": actual_content,
        "error": error,
        "status": status,
        # 已公布未生效：引文本身与库内文本一致（verified 仍为 True），
        # 但绝不能当现行依据引用。给 agent 和前端一个明确的字段，别让它自己猜。
        "is_pending": pending,
        "warning": (
            f"该条文已公布但尚未生效，{effective_date} 起施行，不得作为现行有效依据引用"
            if pending and effective_date else
            ("该条文已公布但尚未生效，不得作为现行有效依据引用" if pending else None)
        ),
        "effective_date": effective_date,
        "repeal_date": repeal_date,
        "repeal_reason": repeal_reason,
        "replaced_by": replaced_by,
        "is_principle_clause": is_principle_clause,
        "has_multiple_versions": has_multiple_versions,
        # 一行并列引文会拆成多条结果，调用方靠这个字段把结果对回原文
        "source_citation": source_citation,
    }


# ============================================================
# 查询内核
# ============================================================

class LawStore:
    """一个 sqlite3 连接上的法条查询集合。

    用法：
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        store = LawStore(conn)
        store.get_article("中华人民共和国刑法", 133, "之一")
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---------- 内部工具 ----------

    def _has_table(self, name: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
            (name,),
        ).fetchone()
        return row is not None

    def _has_column(self, table: str, column: str) -> bool:
        try:
            return any(r[1] == column for r in self.conn.execute(f"PRAGMA table_info({table})"))
        except Exception:  # noqa: BLE001
            return False

    def _history_clause(self, include_history: bool, alias: str = "a") -> str:
        if include_history:
            return ""
        return f" AND ({alias}.status IS NULL OR {alias}.status != 'repealed')"

    def _suffix_clause(self, article_suffix: str) -> tuple[str, tuple]:
        """没指定后缀时只认正条，避免「第八十二条」把「第八十二条之一」串进来。"""
        if article_suffix:
            return " AND article_suffix = ?", (article_suffix,)
        return " AND (article_suffix = '' OR article_suffix IS NULL)", ()

    # ---------- 单条定位 ----------

    def get_article(
        self,
        law_name: str,
        article_num: int,
        article_suffix: str = "",
        include_history: bool = False,
    ) -> dict | None:
        """法名 + 条号 → 条文。精确匹配优先，其次模糊匹配（排除司法解释误命中）。"""
        normalized_name = normalize_law_name(law_name)
        original_name = (law_name or "").strip()
        history_filter = self._history_clause(include_history, "law_articles")
        suffix_filter, suffix_args = self._suffix_clause(article_suffix)

        row = None
        for name_to_try in (original_name, normalized_name):
            row = self.conn.execute(
                f"""SELECT law_name, law_type, law_type_label, article_number, article_number_num,
                          article_suffix, chapter, section, article, content, publish_date,
                          effective_date, status, repeal_date, repeal_reason, replaced_by,
                          is_principle_clause, source_file
                   FROM law_articles
                   WHERE law_name = ? AND article_number_num = ?{history_filter}{suffix_filter}
                   ORDER BY {status_rank()}, id DESC
                   LIMIT 1""",
                (name_to_try, article_num, *suffix_args),
            ).fetchone()
            if row:
                break

        if not row:
            rows = self.conn.execute(
                f"""SELECT law_name, law_type, law_type_label, article_number, article_number_num,
                          article_suffix, chapter, section, article, content, publish_date,
                          effective_date, status, repeal_date, repeal_reason, replaced_by,
                          is_principle_clause, source_file
                   FROM law_articles
                   WHERE (law_name LIKE ? OR law_name LIKE ?)
                     AND article_number_num = ?{history_filter}{suffix_filter}
                   ORDER BY {status_rank()}, LENGTH(law_name) ASC
                   LIMIT 5""",
                (f"%{normalized_name}%", f"%{original_name}%", article_num, *suffix_args),
            ).fetchall()
            # 「解释」类文件常包含被解释法的同名条文，优先取非解释的
            exact_rows = [r for r in rows if "解释" not in r["law_name"]]
            row = exact_rows[0] if exact_rows else (rows[0] if rows else None)

        if not row:
            return None

        return {
            "law_name": row["law_name"],
            "law_type": row["law_type"],
            "law_type_label": row["law_type_label"],
            "article_number": row["article_number"],
            "article_num": row["article_number_num"],
            "article_suffix": row["article_suffix"] or "",
            "chapter": row["chapter"],
            "section": row["section"],
            "article": row["article"],
            "content": row["content"],
            "publish_date": row["publish_date"] or "",
            "effective_date": row["effective_date"] or "",
            "status": row["status"] or "effective",
            "repeal_date": row["repeal_date"] or "",
            "repeal_reason": row["repeal_reason"] or "",
            "replaced_by": row["replaced_by"] or "",
            "is_principle_clause": bool(row["is_principle_clause"]),
            "source_file": row["source_file"],
        }

    def resolve_law_name(self, law_name: str, include_history: bool = True) -> dict:
        """把用户输入的法名解析到库里的规范名称。

        返回 {"law_name": 命中的库内名称 | None, "candidates": [...], "ambiguous": bool}
        精确命中优先；否则取模糊候选里最短的那个（最长公共前缀更长，通常是正主）。
        """
        raw = (law_name or "").strip()
        normalized = normalize_law_name(raw)
        history = self._history_clause(include_history, "law_articles")

        for name_to_try in (raw, normalized):
            row = self.conn.execute(
                f"SELECT law_name FROM law_articles a WHERE a.law_name = ?{history} LIMIT 1",
                (name_to_try,),
            ).fetchone()
            if row:
                return {"law_name": row["law_name"], "candidates": [row["law_name"]], "ambiguous": False}

        rows = self.conn.execute(
            f"""SELECT a.law_name, COUNT(*) AS n
                FROM law_articles a
                WHERE (a.law_name LIKE ? OR a.law_name LIKE ?){history}
                GROUP BY a.law_name
                ORDER BY LENGTH(a.law_name) ASC, n DESC
                LIMIT 10""",
            (f"%{normalized}%", f"%{raw}%"),
        ).fetchall()
        names = [r["law_name"] for r in rows]
        if not names:
            return {"law_name": None, "candidates": [], "ambiguous": False}
        return {"law_name": names[0], "candidates": names, "ambiguous": len(names) > 1}

    # ---------- 整部法规 ----------

    def get_law(
        self,
        law_name: str,
        include_history: bool = False,
        with_content: bool = True,
        max_articles: int = 0,
    ) -> dict | None:
        """法名 → 法规元信息 + 章节目录 + 条文（可只取目录）。

        条文按 (条号, 后缀) 数值排序，不用字符串排序 —— 否则「第十条」会排到「第二条」前面。
        """
        resolved = self.resolve_law_name(law_name, include_history=True)
        if not resolved["law_name"]:
            return None
        exact_name = resolved["law_name"]

        history = self._history_clause(include_history, "a")
        total = self.conn.execute(
            f"SELECT COUNT(*) FROM law_articles a WHERE a.law_name = ?{history}", (exact_name,)
        ).fetchone()[0]
        if total == 0:
            return None

        meta_row = self.conn.execute(
            f"""SELECT a.law_type, a.law_type_label, a.publish_date, a.effective_date,
                       a.status, a.source_file, a.issuing_authority
                FROM law_articles a
                WHERE a.law_name = ?{history}
                ORDER BY {status_rank('a')}, a.id DESC
                LIMIT 1""",
            (exact_name,),
        ).fetchone()

        # 章节目录：**按「章」切段**，不按「节」。
        # 原因：源数据里 section 字段不可靠（实测《劳动合同法》每一条的 section
        # 都被写成最后一节「第三节 非全日制用工」），按 (章,节) 切会碎成一堆假段。
        # 节的信息仍原样保留在每条条文上，只是不参与分组。
        outline: list[dict] = []
        for r in self.conn.execute(
            f"""SELECT a.chapter, a.section, a.article_number_num, a.article_suffix
                FROM law_articles a
                WHERE a.law_name = ?{history}
                ORDER BY a.article_number_num ASC, COALESCE(a.article_suffix,'') ASC""",
            (exact_name,),
        ):
            chapter = r["chapter"] or ""
            if not outline or outline[-1]["chapter"] != chapter:
                outline.append({
                    "chapter": chapter,
                    "start_article": f"{r['article_number_num']}{r['article_suffix'] or ''}",
                    "article_count": 1,
                })
            else:
                outline[-1]["article_count"] += 1

        articles: list[dict] = []
        if with_content:
            limit_sql = " LIMIT ?" if max_articles and max_articles > 0 else ""
            params: tuple = (exact_name,)
            if limit_sql:
                params = (exact_name, max_articles)
            for r in self.conn.execute(
                f"""SELECT a.article_number, a.article_number_num, a.article_suffix,
                           a.chapter, a.section, a.content, a.status
                    FROM law_articles a
                    WHERE a.law_name = ?{history}
                    ORDER BY a.article_number_num ASC, COALESCE(a.article_suffix,'') ASC{limit_sql}""",
                params,
            ):
                articles.append({
                    "article_number": r["article_number"],
                    "article_num": r["article_number_num"],
                    "article_suffix": r["article_suffix"] or "",
                    "chapter": r["chapter"] or "",
                    "section": r["section"] or "",
                    "content": r["content"],
                    "status": r["status"] or "effective",
                })

        # 版本沿革：优先读 law_versions（有 version_id 的新库），老库降级为空
        versions: list[dict] = []
        if self._has_table("law_versions"):
            for r in self.conn.execute(
                """SELECT id, law_name, publish_date, effective_date, status, article_count,
                          superseded_at
                   FROM law_versions
                   WHERE law_name = ? OR norm_name = ?
                   ORDER BY COALESCE(publish_date,'') DESC, id DESC""",
                (exact_name, normalize_law_name(exact_name)),
            ):
                versions.append({
                    "version_id": r["id"],
                    "publish_date": r["publish_date"] or "",
                    "effective_date": r["effective_date"] or "",
                    "status": r["status"] or "effective",
                    "article_count": r["article_count"] or 0,
                    "superseded_at": r["superseded_at"] or "",
                })

        return {
            "law_name": exact_name,
            "law_type": meta_row["law_type"] if meta_row else None,
            "law_type_label": meta_row["law_type_label"] if meta_row else None,
            "publish_date": (meta_row["publish_date"] or "") if meta_row else "",
            "effective_date": (meta_row["effective_date"] or "") if meta_row else "",
            "status": (meta_row["status"] or "effective") if meta_row else "effective",
            "issuing_authority": (meta_row["issuing_authority"] or "") if meta_row else "",
            "source_file": (meta_row["source_file"] or "") if meta_row else "",
            "article_count": total,
            "outline": outline,
            "versions": versions,
            "articles": articles,
            "name_ambiguous": resolved["ambiguous"],
            "name_candidates": resolved["candidates"],
        }

    # ---------- 检索 ----------

    def search(
        self,
        q: str,
        k: int = 10,
        law_type: str | None = None,
        include_history: bool = False,
        law_name: str | None = None,
    ) -> list[dict]:
        """三级检索：FTS5(trigram+bm25) → LIKE → 2 字片段多数命中。

        第三级是为中文准备的：用户写「二倍工资」而原文是「二倍的工资」时，
        字面连续匹配必然落空，只能拆片段换召回。precision 让一点，换可用性。
        """
        q = (q or "").strip()
        if not q:
            return []

        filters = ""
        params: list = []
        if law_type:
            filters += " AND a.law_type = ?"
            params.append(law_type)
        if law_name:
            filters += " AND a.law_name LIKE ?"
            params.append(f"%{normalize_law_name(law_name)}%")
        if not include_history:
            filters += " AND (a.status IS NULL OR a.status != 'repealed')"

        # 1) FTS5
        fts = fts_query(q)
        if fts:
            try:
                rows = self.conn.execute(
                    f"""SELECT {_COLS}, bm25(law_articles_fts) AS rank
                        FROM law_articles_fts
                        JOIN law_articles a ON a.id = law_articles_fts.rowid
                        WHERE law_articles_fts MATCH ?{filters}
                        ORDER BY rank
                        LIMIT ?""",
                    (fts, *params, k),
                ).fetchall()
                if rows:
                    # bm25 越小越相关，转成正向 0~1 分数便于展示
                    return dedupe_versions(
                        [row_to_item(r, round(1.0 / (1.0 + abs(r["rank"])), 4)) for r in rows]
                    )
            except Exception:  # noqa: BLE001
                # FTS 表缺失或语法异常时静默降级，不让检索整体失败
                pass

        # 2) LIKE 兜底
        rows = self.conn.execute(
            f"""SELECT {_COLS}
                FROM law_articles a
                WHERE (a.content LIKE ? OR a.article LIKE ?){filters}
                LIMIT ?""",
            (f"%{q}%", f"%{q}%", *params, k),
        ).fetchall()
        if rows:
            return dedupe_versions([row_to_item(r) for r in rows])

        # 3) 片段兜底
        terms = match_terms(q)
        if terms:
            n = len(terms)
            need = max(2, math.ceil(n * 0.6)) if n > 1 else 1
            score_sql = " + ".join("(a.content LIKE ?)" for _ in terms)
            rows = self.conn.execute(
                f"""SELECT * FROM (
                        SELECT {_COLS}, {score_sql} AS hits
                        FROM law_articles a
                        WHERE 1 = 1{filters}
                    ) WHERE hits >= ?
                    ORDER BY hits DESC, LENGTH(content) ASC
                    LIMIT ?""",
                (*[f"%{x}%" for x in terms], *params, need, k),
            ).fetchall()
            return dedupe_versions([row_to_item(r, round(r["hits"] / n, 4)) for r in rows])

        return []

    def search_multi(
        self,
        terms: list[str],
        mode: str = "AND",
        k: int = 10,
        law_type: str | None = None,
        include_history: bool = False,
    ) -> list[dict]:
        """多关键词检索。AND 要求全部命中，OR 命中任意一个。

        给 agent 用的形态：它先把人话拆成关键词，再决定要严还是要松。
        """
        terms = [t.strip() for t in (terms or []) if t and t.strip()]
        if not terms:
            return []

        filters = ""
        params: list = []
        if law_type:
            filters += " AND a.law_type = ?"
            params.append(law_type)
        if not include_history:
            filters += " AND (a.status IS NULL OR a.status != 'repealed')"

        join = " AND " if mode.upper() == "AND" else " OR "
        where = join.join("(a.content LIKE ? OR a.article LIKE ?)" for _ in terms)
        args: list = []
        for t in terms:
            args.extend([f"%{t}%", f"%{t}%"])

        rows = self.conn.execute(
            f"""SELECT {_COLS}
                FROM law_articles a
                WHERE ({where}){filters}
                ORDER BY LENGTH(a.content) ASC
                LIMIT ?""",
            (*args, *params, k),
        ).fetchall()
        return dedupe_versions([row_to_item(r) for r in rows])

    def list_laws(self, q: str = "", k: int = 20, law_type: str | None = None) -> list[dict]:
        """按法名列出法规（带条数），用于让 agent 先确认到底查哪一部。"""
        filters = ""
        params: list = []
        if q:
            filters += " AND law_name LIKE ?"
            params.append(f"%{normalize_law_name(q)}%")
        if law_type:
            filters += " AND law_type = ?"
            params.append(law_type)

        rows = self.conn.execute(
            f"""SELECT law_name, law_type, law_type_label,
                       MIN(publish_date) AS publish_date, MAX(effective_date) AS effective_date,
                       COUNT(*) AS article_count
                FROM law_articles
                WHERE (status IS NULL OR status != 'repealed'){filters}
                GROUP BY law_name
                ORDER BY article_count DESC
                LIMIT ?""",
            (*params, k),
        ).fetchall()
        return [
            {
                "law_name": r["law_name"],
                "law_type": r["law_type"],
                "law_type_label": r["law_type_label"],
                "publish_date": r["publish_date"] or "",
                "effective_date": r["effective_date"] or "",
                "article_count": r["article_count"],
            }
            for r in rows
        ]

    # ---------- 引文核验 ----------

    def verify(self, citations: list, include_history: bool = False) -> dict:
        """批量核验引用是否存在、是否现行、是否多版本歧义。"""
        results: list[dict] = []
        history_filter = self._history_clause(include_history, "law_articles")

        # 并列引文（「第八十二条、第八十七条」）先展开，一行引两个条文就得核两次。
        # 放在这里而不是前端，是为了让 HTTP 与 MCP 两条路径行为一致。
        expanded: list = []
        for c in citations or []:
            if isinstance(c, str):
                for item in expand_citation(c):
                    expanded.append((item, c))
            else:
                expanded.append((c, ""))

        for citation, source_text in expanded:
            if isinstance(citation, dict):
                law_name = (citation.get("law_name") or "").strip()
                article_number_str = (citation.get("article_number") or "").strip()
                article_num = parse_article_number(article_number_str)
                article_suffix = (citation.get("article_suffix") or "").strip() \
                    or parse_article_suffix(article_number_str)
            elif isinstance(citation, str):
                parsed = parse_citation_string(citation)
                law_name = parsed["law_name"]
                article_num = parsed["article_num"]
                article_number_str = parsed.get("article_number", "")
                article_suffix = parsed.get("article_suffix", "")
            else:
                results.append(_build_verify_result(
                    law_name=str(citation), article_number="",
                    verified=False, error="invalid_citation_format",
                    source_citation=source_text or str(citation),
                ))
                continue

            if not law_name or article_num <= 0:
                results.append(_build_verify_result(
                    law_name=law_name, article_number=article_number_str,
                    verified=False, error="parse_failed",
                    source_citation=source_text,
                ))
                continue

            normalized = normalize_law_name(law_name)
            suffix_filter, suffix_args = self._suffix_clause(article_suffix)
            rows = self.conn.execute(
                f"""SELECT law_name, article_number, article_number_num, article_suffix,
                          version_id, content, publish_date, effective_date, status,
                          repeal_date, repeal_reason, replaced_by, is_principle_clause
                   FROM law_articles
                   WHERE (law_name LIKE ? OR law_name LIKE ?)
                     AND article_number_num = ?{history_filter}{suffix_filter}
                   ORDER BY CASE WHEN law_name = ? OR law_name = ? THEN 0 ELSE 1 END,
                            {status_rank()}, LENGTH(law_name) ASC""",
                (f"%{normalized}%", f"%{law_name}%", article_num, *suffix_args,
                 normalized, law_name),
            ).fetchall()

            if not rows:
                results.append(_build_verify_result(
                    law_name=law_name, article_number=article_number_str,
                    verified=False, error="未找到该法条",
                    source_citation=source_text,
                ))
                continue

            non_interpretation = [r for r in rows if "解释" not in r["law_name"]]
            selected_rows = non_interpretation if non_interpretation else rows

            # 多版本歧义只看同一部法规（同名）是否还有别的版本记录
            row = selected_rows[0]
            same_law = [r for r in selected_rows if r["law_name"] == row["law_name"]]
            versions = {(r["version_id"], r["publish_date"] or "") for r in same_law}
            row_status = row["status"] or "effective"
            row_eff = row["effective_date"] or ""
            if row_status == "pending" and row_eff and row_eff <= _today():
                row_status = "effective"
            results.append(_build_verify_result(
                law_name=row["law_name"],
                article_number=row["article_number"],
                article_suffix=row["article_suffix"] or "",
                verified=True,
                actual_content=row["content"],
                status=row_status,
                effective_date=row_eff,
                repeal_date=row["repeal_date"] or "",
                repeal_reason=row["repeal_reason"] or "",
                replaced_by=row["replaced_by"] or "",
                is_principle_clause=bool(row["is_principle_clause"]),
                has_multiple_versions=len(versions) > 1,
                source_citation=source_text,
            ))

        return {
            "results": results,
            "total": len(results),
            "verified_count": sum(1 for r in results if r.get("verified")),
            "failed_count": sum(1 for r in results if not r.get("verified")),
        }

    # ---------- 库状态 ----------

    def stats(self) -> dict:
        """库统计。老库没有 law_versions 时自动降级为条文表口径。"""
        out: dict = {}
        out["articles_total"] = self.conn.execute("SELECT COUNT(*) FROM law_articles").fetchone()[0]
        # pending 必须单独扣出来：算进 effective 会让「现行有效条文数」虚高，
        # 而这个数会直接写进 README、MCP 返回和对外文案。
        out["articles_pending"] = self.conn.execute(
            "SELECT COUNT(*) FROM law_articles WHERE status = 'pending'"
        ).fetchone()[0]
        out["articles_effective"] = self.conn.execute(
            "SELECT COUNT(*) FROM law_articles "
            "WHERE status IS NULL OR status NOT IN ('repealed','pending')"
        ).fetchone()[0]
        out["articles_repealed"] = (
            out["articles_total"] - out["articles_effective"] - out["articles_pending"]
        )

        if self._has_table("law_versions"):
            out["law_versions"] = self.conn.execute("SELECT COUNT(*) FROM law_versions").fetchone()[0]
            out["laws"] = self.conn.execute(
                "SELECT COUNT(DISTINCT norm_name) FROM law_versions WHERE status = 'effective'"
            ).fetchone()[0]
            # 含未生效的法规数：用户问「库里有多少部法」时，pending 也算收了
            out["laws_pending"] = self.conn.execute(
                "SELECT COUNT(DISTINCT norm_name) FROM law_versions WHERE status = 'pending'"
            ).fetchone()[0]
        else:
            out["law_versions"] = 0
            out["laws_pending"] = 0
            out["laws"] = self.conn.execute(
                "SELECT COUNT(DISTINCT law_name) FROM law_articles"
            ).fetchone()[0]

        by_type = {}
        for r in self.conn.execute(
            """SELECT COALESCE(law_type_label, '') AS label, COUNT(*) AS n
               FROM law_articles
               WHERE status IS NULL OR status != 'repealed'
               GROUP BY label ORDER BY n DESC"""
        ):
            by_type[r["label"] or "未分类"] = r["n"]
        out["by_law_type"] = by_type

        if self._has_table("corpus_meta"):
            out["corpus_meta"] = {
                r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM corpus_meta")
            }
        else:
            out["corpus_meta"] = {}
        return out
