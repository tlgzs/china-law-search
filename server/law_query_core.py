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

try:  # 以 `uvicorn server.law_search_service:app` 启动
    from . import law_state  # noqa: PLC0415
except ImportError:  # 以 `python mcp/server.py` 直接把 server/ 放进 sys.path
    import law_state  # type: ignore[no-redef]  # noqa: PLC0415


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


def resolve_law_names(conn, name: str, limit: int = 3) -> list:
    """把用户写的法规名换成库里条文的正式全称。

    条文的 `law_name` 保留原文的书名号（「…关于废止《海南省环境保护条例》的决定」），
    而 `law_versions.norm_name` 是去掉书名号的归一化名。用户输入一般不带书名号，
    于是 `law_name = ?` 对不上；`LIKE '%…%'` 也救不回来 —— 书名号把子串切断了，
    「废止海南省环境保护条例的决定」在库里并不是一段连续文本。

    所以先在 law_versions（一万多行）里反查全称，再拿全称回条文表精确取。
    先按 norm_name 精确匹配，再退到忽略书名号的比较。
    """
    target = normalize_law_name(name)
    if not target:
        return []

    rows = conn.execute(
        "SELECT DISTINCT law_name FROM law_versions WHERE norm_name = ? "
        "ORDER BY COALESCE(publish_date, '') DESC LIMIT ?",
        (target, limit),
    ).fetchall()

    if not rows:
        rows = conn.execute(
            "SELECT DISTINCT law_name FROM law_versions "
            "WHERE REPLACE(REPLACE(norm_name, '《', ''), '》', '') = ? "
            "ORDER BY COALESCE(publish_date, '') DESC LIMIT ?",
            (target, limit),
        ).fetchall()

    out = []
    for r in rows:
        v = r["law_name"] if hasattr(r, "keys") else r[0]
        if v and v not in out:
            out.append(v)
    return out


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
        "law_level": (r["law_level"] if "law_level" in r.keys() else "") or "",
        "law_province": (r["law_province"] if "law_province" in r.keys() else "") or "",
        "law_city": (r["law_city"] if "law_city" in r.keys() else "") or "",
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
           a.publish_date, a.effective_date, a.status, a.repeal_date, a.replaced_by,
           a.law_level, a.law_province, a.law_city"""


# ============================================================
# 二字倒排索引读取（写入与构建在 ingest/schema.py）
# ============================================================

_BIGRAM_META_KEY = "bigram_index_version"
_BIGRAM_INDEX_VERSION = "1"


def _decode_ids(blob: bytes):
    """增量 varint → 条文 id。与 ingest/schema.py 的 _encode_ids 互逆。"""
    num = 0
    shift = 0
    total = 0
    for b in blob:
        num |= (b & 0x7F) << shift
        if b & 0x80:
            shift += 7
        else:
            total += num
            yield total
            num = 0
            shift = 0


def _merge_sorted(left, right):
    a = next(left, None)
    b = next(right, None)
    while a is not None or b is not None:
        if b is None or (a is not None and a <= b):
            yield a
            a = next(left, None)
        else:
            yield b
            b = next(right, None)


def bigram_query_ok(q: str) -> bool:
    """只有「恰好 2 个字」、且不含 LIKE 通配符与 ASCII 字母的查询才走倒排。

    LIKE 对 ASCII 字母大小写不敏感，倒排索引区分大小写；含 % 或 _ 时 LIKE 的
    语义也不一样。这几种情况一律退回原来的 LIKE，保证结果集不变。
    """
    if len(q) != 2:
        return False
    if "%" in q or "_" in q:
        return False
    return not re.search(r"[A-Za-z]", q)


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
    law_status: str | None = None,
    article_status: str | None = None,
    citation_valid: bool = False,
    version: dict | None = None,
    message: str | None = None,
    previous_version: dict | None = None,
    previous_content: str | None = None,
    previous_article_number: str | None = None,
    renumber_candidates: list | None = None,
) -> dict:
    """构建校验结果，确保所有字段都返回。

    新旧字段并存：status / is_pending / is_repealed 保持旧调用方可用，
    law_status / article_status / citation_valid / version 是本次新增的
    唯一判据。上层判断「能不能引」只看 citation_valid，不要再组合 status。
    """
    pending = status == "pending"

    if message:
        warning = message
    elif pending:
        warning = (f"该条文已公布但尚未生效，{effective_date} 起施行，"
                   "不得作为现行有效依据引用") if effective_date else \
                  "该条文已公布但尚未生效，不得作为现行有效依据引用"
    elif status == "repealed" and law_status != law_state.LAW_EFFECTIVE:
        # 整部法废止才说「该法规已废止」；条文单独废止不能说成整部法废止。
        basis = repeal_reason or ""
        if basis.startswith("被") and basis.endswith("废止"):
            basis = basis[1:-2]          # 剥掉「被…废止」的外壳，避免「废止（…废止）」
        if basis:
            warning = (f"该法规已于 {repeal_date} 废止，依据：{basis}，不得作为现行依据引用"
                       if repeal_date else
                       f"该法规已废止，依据：{basis}，不得作为现行依据引用")
        else:
            warning = "该法规已废止，不得作为现行依据引用"
    elif status == "repealed":
        warning = "该条文已被单独废止，不得作为现行有效依据引用"
    else:
        warning = None

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
        "warning": warning,
        "effective_date": effective_date,
        "repeal_date": repeal_date,
        "repeal_reason": repeal_reason,
        "replaced_by": replaced_by,
        "is_principle_clause": is_principle_clause,
        "has_multiple_versions": has_multiple_versions,
        # 一行并列引文会拆成多条结果，调用方靠这个字段把结果对回原文
        "source_citation": source_citation,
        # 本次统一后的语义字段
        "law_status": law_status,
        "article_status": article_status,
        "citation_valid": citation_valid,
        "version": version,
        "previous_version": previous_version,
        "previous_content": previous_content,
        "previous_article_number": previous_article_number,
        "renumber_candidates": renumber_candidates or [],
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
        # 二字索引是否可用，第一次查表后缓存；库在会话中途变化也不重查
        self._bigram_flag: bool | None = None

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

    # ---------- 二字倒排索引 ----------

    def _bigram_ready(self) -> bool:
        """索引存在且版本标记对得上才用；否则退回 LIKE（宁可慢，不能漏）。"""
        if self._bigram_flag is None:
            ok = False
            if self._has_table("law_bigram_index"):
                try:
                    row = self.conn.execute(
                        "SELECT value FROM corpus_meta WHERE key = ?",
                        (_BIGRAM_META_KEY,)).fetchone()
                    ok = bool(row) and str(row[0]) == _BIGRAM_INDEX_VERSION
                except Exception:  # noqa: BLE001
                    ok = False
            self._bigram_flag = ok
        return self._bigram_flag

    def _iter_bigram_ids(self, gram: str):
        """按 id 升序产出含该二字组的条文 id。

        主索引是打包时写死的 postings，增量包只追加到 law_bigram_add，
        所以两路必须归并，不能只读其中一路——否则更新过的库会漏掉新条文。
        """
        row = self.conn.execute(
            "SELECT postings FROM law_bigram_index WHERE gram = ?", (gram,)).fetchone()
        main = _decode_ids(row[0]) if row and row[0] else iter(())
        extra = iter(())
        if self._has_table("law_bigram_add"):
            extra = (r[0] for r in self.conn.execute(
                "SELECT article_id FROM law_bigram_add WHERE gram = ? ORDER BY article_id",
                (gram,)))
        return _merge_sorted(main, extra)

    def _lookup_by_ids(self, ids: list[int], q: str, filters: str, params: list,
                       limit: int) -> list:
        """按 id 批查，谓词与原 LIKE 完全相同，保证结果集与修前逐条一致。"""
        placeholders = ",".join("?" for _ in ids)
        return self.conn.execute(
            f"""SELECT {_COLS} FROM law_articles a
                WHERE a.id IN ({placeholders})
                  AND (a.content LIKE ? OR a.article LIKE ?){filters}
                ORDER BY a.id
                LIMIT ?""",
            (*ids, f"%{q}%", f"%{q}%", *params, limit),
        ).fetchall()

    # ---------- 单条定位 ----------

    def get_article(
        self,
        law_name: str,
        article_num: int,
        article_suffix: str = "",
        include_history: bool = False,
        as_of: str | None = None,
    ) -> dict | None:
        """法名 + 条号 → 条文与状态。

        状态判定统一走 server/law_state.py：HTTP /api/law/article 与 MCP
        get_article 共用这一份，不再各写一套「查不到就当整部废止」。
        `as_of` 仍按时间点取版本（与状态推导是两件事），保持原行为。
        """
        if as_of:
            return self._get_article_as_of(law_name, article_num, article_suffix, as_of)

        candidates = self._name_candidates(law_name)
        result = law_state.resolve_article(
            self.conn, candidates, article_num, article_suffix,
            include_history=include_history,
            article_number_text=f"第{article_num}条{article_suffix}",
        )
        if result is None:
            # 精确名与归一化名都找不到，再做一次宽松匹配（「民法典」这类简称）
            resolved = self.resolve_law_name(law_name, include_history=True)
            extra = [resolved.get("law_name")] + list(resolved.get("candidates") or [])
            extra = [n for n in extra if n and n not in candidates]
            if extra:
                candidates = candidates + extra
                result = law_state.resolve_article(
                    self.conn, candidates, article_num, article_suffix,
                    include_history=include_history,
                    article_number_text=f"第{article_num}条{article_suffix}",
                )
        if result is None:
            return None
        self._attach_law_meta(result)
        return result

    def _name_candidates(self, law_name: str) -> list[str]:
        """法名候选：原样、去书名号、law_versions 里反查到的正式全称。"""
        candidates = [law_name, normalize_law_name(law_name)]
        for canonical in resolve_law_names(self.conn, law_name):
            if canonical not in candidates:
                candidates.append(canonical)
        return [n for n in candidates if n]

    def _attach_law_meta(self, result: dict) -> None:
        """补上旧接口一直返回的展示字段（状态模块不关心这些）。"""
        row = self.conn.execute(
            "SELECT law_type, law_type_label, source_file FROM law_articles "
            "WHERE law_name = ? ORDER BY id DESC LIMIT 1",
            (result["law_name"],),
        ).fetchone()
        result.setdefault("law_type", row["law_type"] if row else None)
        result.setdefault("law_type_label", row["law_type_label"] if row else None)
        result.setdefault("source_file", row["source_file"] if row else "")
        if "version_id" not in result:
            result["version_id"] = (result.get("version") or {}).get("version_id")

    @staticmethod
    def _article_item(row) -> dict:
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

    def list_versions(self, law_name: str) -> list[dict]:
        """该法在库里的版本清单，按施行日升序，附各自的有效区间。

        区间右端点是**下一版的施行日（不含）**：新版当天生效，旧版当天即失效，
        所以「2019-06-01 这天适用哪版」用 `effective_date <= as_of < in_force_until` 判定。
        """
        resolved = self.resolve_law_name(law_name, include_history=True)
        name = resolved.get("law_name") or (law_name or "").strip()
        rows = list(self.conn.execute(
            """SELECT id, law_name, publish_date, effective_date, status, article_count
               FROM law_versions
               WHERE law_name = ?
               ORDER BY COALESCE(effective_date, publish_date, '') ASC, id ASC""",
            (name,),
        ).fetchall())
        if not rows and name != (law_name or "").strip():
            # 归一化名对不上时退回模糊，避免用户少写书名号就查不到版本区间
            rows = list(self.conn.execute(
                """SELECT id, law_name, publish_date, effective_date, status, article_count
                   FROM law_versions
                   WHERE law_name LIKE ?
                   ORDER BY COALESCE(effective_date, publish_date, '') ASC, id ASC LIMIT 20""",
                (f"%{normalize_law_name(law_name)}%",),
            ).fetchall())

        out: list[dict] = []
        for i, r in enumerate(rows):
            start = r["effective_date"] or r["publish_date"] or ""
            nxt = rows[i + 1] if i + 1 < len(rows) else None
            until = (nxt["effective_date"] or nxt["publish_date"] or "") if nxt else ""
            out.append({
                "version_id": r["id"],
                "law_name": r["law_name"],
                "publish_date": r["publish_date"] or "",
                "effective_date": start,
                "in_force_until": until,   # 空 = 暂无下一版（一直是最后一版）
                "status": r["status"] or "",
                "article_count": r["article_count"] or 0,
            })
        return out

    def _get_article_as_of(self, law_name: str, article_num: int,
                           article_suffix: str, as_of: str) -> dict | None:
        """按时间点取条文：办旧案子要引当年施行的版本，不能拿最新版套。

        版本选择与状态推导都交给 law_state，保证与默认查询同一套语义：
        历史行上的 repealed 只是「被新版取代」，不能回成「已废止」。
        """
        day = (as_of or "").strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            return {"error": "invalid_as_of", "as_of": day,
                    "message": "as_of 需为 YYYY-MM-DD 格式的日期。", "available_versions": []}

        versions = self.list_versions(law_name)
        if not versions:
            return None

        candidates = self._name_candidates(law_name)
        result = law_state.resolve_article(
            self.conn, candidates, article_num, article_suffix,
            article_number_text=f"第{article_num}条{article_suffix}", as_of=day,
        )
        if result is None:
            return None

        self._attach_law_meta(result)
        result["as_of"] = day
        if result.get("error"):
            result["law_name"] = result.get("law_name") or versions[0]["law_name"]
            result["available_versions"] = versions
            return result
        result["version_id"] = (result.get("version") or {}).get("version_id")
        result["version_effective_date"] = \
            (result.get("version") or {}).get("effective_date", "")
        return result

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
        law_level: str | None = None,
        province: str | None = None,
        city: str | None = None,
    ) -> list[dict]:
        """三级检索：FTS5(trigram+bm25) → 二字倒排 → LIKE → 2 字片段多数命中。

        第三级是为中文准备的：用户写「二倍工资」而原文是「二倍的工资」时，
        字面连续匹配必然落空，只能拆片段换召回。precision 让一点，换可用性。

        `law_level`（national/province/city）、`province`、`city` 来自名称前缀推导，
        用于「只查某省/某市的法规」；字段为空的历史数据不会被这些过滤命中。
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
        if law_level:
            filters += " AND a.law_level = ?"
            params.append(law_level)
        if province:
            filters += " AND a.law_province = ?"
            params.append(province)
        if city:
            filters += " AND a.law_city = ?"
            params.append(city)
        if not include_history:
            filters += " AND (a.status IS NULL OR a.status != 'repealed')"

        # 1) FTS5
        fts = fts_query(q)
        if fts:
            # include_history=False 时先多取 3 倍候选：同一部法新旧两版并存时，
            # 新版（pending）按 bm25 也可能排进前 k，把现行版整条挤出结果。
            # 取宽一点再在内存里滤掉 pending、截回 k，排序与 dedupe 规则不变。
            fetch_k = k if include_history else k * 3
            try:
                rows = self.conn.execute(
                    f"""SELECT {_COLS}, bm25(law_articles_fts) AS rank
                        FROM law_articles_fts
                        JOIN law_articles a ON a.id = law_articles_fts.rowid
                        WHERE law_articles_fts MATCH ?{filters}
                        ORDER BY rank
                        LIMIT ?""",
                    (fts, *params, fetch_k),
                ).fetchall()
                if rows:
                    # bm25 越小越相关，转成正向 0~1 分数便于展示
                    items = [row_to_item(r, round(1.0 / (1.0 + abs(r["rank"])), 4)) for r in rows]
                    if not include_history:
                        current = [it for it in items if not it.get("is_pending")]
                        # 命中的全是「已公布未生效」的新规时退回原结果，别让检索变空
                        items = current or items
                    return dedupe_versions(items)[:k]
            except Exception:  # noqa: BLE001
                # FTS 表缺失或语法异常时静默降级，不让检索整体失败
                pass

        # 2) 二字查询走倒排索引：trigram 只切 3 字窗口，2 字走 LIKE 是全表扫描
        #    （实测 2.8–10.8 秒）。倒排按 id 升序返回，落库仍用原 LIKE 谓词复核，
        #    所以结果数量与顺序跟修前逐条一致，不是"只查精确匹配"。
        if bigram_query_ok(q) and self._bigram_ready():
            found: list = []
            batch: list[int] = []
            for aid in self._iter_bigram_ids(q):
                batch.append(aid)
                if len(batch) >= 200:
                    rest = k - len(found)
                    if rest > 0:
                        found.extend(self._lookup_by_ids(batch, q, filters, params, rest))
                    batch = []
                    if len(found) >= k:
                        break
            rest = k - len(found)
            if batch and rest > 0:
                found.extend(self._lookup_by_ids(batch, q, filters, params, rest))
            if found:
                return dedupe_versions([row_to_item(r) for r in found])
            # 索引是全量的：候选为空就等于没有命中（含被过滤器筛空的情况），
            # 再退回 LIKE 只会把"查不到"从毫秒拖成数秒（实测 2.4–3.2 秒）。
            return []

        # 3) LIKE 兜底
        rows = self.conn.execute(
            f"""SELECT {_COLS}
                FROM law_articles a
                WHERE (a.content LIKE ? OR a.article LIKE ?){filters}
                LIMIT ?""",
            (f"%{q}%", f"%{q}%", *params, k),
        ).fetchall()
        if rows:
            return dedupe_versions([row_to_item(r) for r in rows])

        # 4) 片段兜底
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
        law_level: str | None = None,
        province: str | None = None,
        city: str | None = None,
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
        if law_level:
            filters += " AND a.law_level = ?"
            params.append(law_level)
        if province:
            filters += " AND a.law_province = ?"
            params.append(province)
        if city:
            filters += " AND a.law_city = ?"
            params.append(city)
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

            # 状态判定统一交给 law_state，与 /api/law/article、MCP get_article
            # 共用同一套规则；这里只负责把结果翻译成核验结构。
            candidates = self._name_candidates(law_name)
            res = law_state.resolve_article(
                self.conn, candidates, article_num, article_suffix,
                include_history=include_history,
                article_number_text=article_number_str,
            )
            if res is None:
                # 精确名都不存在，才放宽成包含匹配（处理「民法典」这类简称）
                resolved = self.resolve_law_name(law_name, include_history=True)
                extra = [resolved.get("law_name")] + list(resolved.get("candidates") or [])
                extra = [n for n in extra if n and n not in candidates]
                if extra:
                    res = law_state.resolve_article(
                        self.conn, candidates + extra, article_num, article_suffix,
                        include_history=include_history,
                        article_number_text=article_number_str,
                    )

            if res is None or res.get("error") == "not_found":
                results.append(_build_verify_result(
                    law_name=(res or {}).get("law_name") or law_name,
                    article_number=article_number_str,
                    verified=False,
                    error=(res or {}).get("message") or "未找到该法条",
                    law_status=(res or {}).get("law_status"),
                    version=(res or {}).get("version"),
                    source_citation=source_text,
                ))
                continue

            article_status = res.get("article_status")

            if article_status == law_state.ARTICLE_IN_FORCE:
                results.append(_build_verify_result(
                    law_name=res["law_name"],
                    article_number=res["article_number"],
                    article_suffix=res["article_suffix"],
                    verified=True,
                    actual_content=res["content"],
                    status=res["status"],
                    effective_date=res["effective_date"],
                    repeal_date=res["repeal_date"],
                    repeal_reason=res["repeal_reason"],
                    replaced_by=res["replaced_by"],
                    is_principle_clause=res["is_principle_clause"],
                    has_multiple_versions=res["has_multiple_versions"],
                    source_citation=source_text,
                    law_status=res["law_status"],
                    article_status=article_status,
                    citation_valid=res["citation_valid"],
                    version=res["version"],
                    message=res.get("message"),
                    previous_version=res.get("previous_version"),
                    previous_content=res.get("previous_content"),
                    previous_article_number=res.get("previous_article_number"),
                    renumber_candidates=res.get("renumber_candidates"),
                ))
                continue

            errors = {
                law_state.ARTICLE_REMOVED: "现行版已删除该条",
                law_state.ARTICLE_RENUMBERED: "该条号已在现行版调整",
                law_state.ARTICLE_REPEALED: (
                    "该法规已废止"
                    if res["law_status"] == law_state.LAW_REPEALED
                    else "该条文已废止"
                ),
                law_state.ARTICLE_UNKNOWN: "未收录现行版，无法确认",
            }
            results.append(_build_verify_result(
                law_name=res["law_name"],
                article_number=res["article_number"],
                article_suffix=res["article_suffix"],
                verified=False,
                error=errors.get(article_status, "未找到该法条"),
                status=res["status"],
                effective_date=res["effective_date"],
                repeal_date=res["repeal_date"],
                repeal_reason=res["repeal_reason"],
                replaced_by=res["replaced_by"],
                is_principle_clause=res["is_principle_clause"],
                has_multiple_versions=res["has_multiple_versions"],
                source_citation=source_text,
                law_status=res["law_status"],
                article_status=article_status,
                citation_valid=res["citation_valid"],
                version=res["version"],
                message=res.get("message"),
                previous_version=res.get("previous_version"),
                previous_content=res.get("previous_content"),
                previous_article_number=res.get("previous_article_number"),
                renumber_candidates=res.get("renumber_candidates"),
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
