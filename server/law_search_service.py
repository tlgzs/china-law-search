"""China Law Search API — 中国法律法规检索 API（双库架构）。

ChromaDB 向量语义检索 + SQLite 精确查找，双库互补，向量库不可用自动降级 FTS。

端口: 8001（可用 LAW_PORT 修改）
数据库（路径全部可用环境变量覆盖，默认放在项目 data/ 目录下）:
  - SQLite:   ./data/law_data.db          (精确查找 + FTS 全文检索 + 法条校验)
  - ChromaDB: ./data/legal_vector_db      (向量语义检索，可选)
端点:
  GET  /api/law/search?q=X&k=Y&type=Z         向量语义检索（降级为 SQLite FTS）
  GET  /api/law/article?law_name=X&article_num=Y  精确条号查找
  POST /api/law/verify                         法条批量校验
  GET  /api/law/stats                          库统计信息
  GET  /health                                 健康检查（免认证）

认证（可选，默认开启）:
  - 每渠道独立 Bearer Token，配置于 api_keys.json（格式见 README）
  - LAW_API_ENFORCE=0 可临时切回软认证（带 token 记来源，不带 token 放行记 unknown）
  - /health 始终免认证（监控用）

渠道分级:
  - tier=private（默认）：不限流
  - tier=public：按 IP 限流（rate_limit_per_min + rate_limit_per_day），超过 429+Retry-After

统计:
  - 每次调用追加一条 JSONL 至 call_log.jsonl
  - 字段：ts, client_id, endpoint, query, result_count, elapsed_ms, status, ip, ua

环境变量（均可选）:
  LAW_DB_PATH          SQLite 路径          默认 <项目>/data/law_data.db
  LAW_CHROMA_PATH      ChromaDB 路径        默认 <项目>/data/legal_vector_db
  LAW_COLLECTION_NAME  Chroma 集合名        默认 chinese_laws
  LAW_EMBEDDING_MODEL  Embedding 模型       默认 BAAI/bge-large-zh-v1.5
  LAW_API_KEYS_FILE    API Key 配置         默认 <项目>/data/api_keys.json
  LAW_CALL_LOG_PATH    调用日志             默认 <项目>/data/call_log.jsonl
  LAW_API_ENFORCE      强制认证             默认 1
  LAW_PORT             监听端口             默认 8001
"""

import datetime
import json
import logging
import math
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

# 离线模式：服务器无外网时，模型已缓存在本地
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request

from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("law_search")

# ============================================================
# 配置（环境变量 > 默认值；默认相对本项目 data/ 目录）
# ============================================================

_BASE_DIR = Path(__file__).resolve().parent.parent
_DATA_DIR = Path(os.environ.get("LAW_DATA_DIR", str(_BASE_DIR / "data")))

DB_PATH = os.environ.get("LAW_DB_PATH", str(_DATA_DIR / "law_data.db"))
CHROMA_PATH = os.environ.get("LAW_CHROMA_PATH", str(_DATA_DIR / "legal_vector_db"))
COLLECTION_NAME = os.environ.get("LAW_COLLECTION_NAME", "chinese_laws")
EMBEDDING_MODEL = os.environ.get("LAW_EMBEDDING_MODEL", "BAAI/bge-large-zh-v1.5")

# 认证与统计
API_KEYS_FILE = os.environ.get("LAW_API_KEYS_FILE", str(_DATA_DIR / "api_keys.json"))
CALL_LOG_PATH = os.environ.get("LAW_CALL_LOG_PATH", str(_DATA_DIR / "call_log.jsonl"))
# 强制模式：1=无 token/错 token → 401；0=软认证（仅记来源，不挡）
API_ENFORCE = os.environ.get("LAW_API_ENFORCE", "0") not in ("0", "false", "False", "")


# ============================================================
# API Key 加载与校验
# ============================================================

def load_api_keys() -> dict[str, dict]:
    """读取配置，反转为 {token: meta} 映射。

    两种格式兼容：
      A) {"client_id": "raw_token_string"}      → private tier，不限流
      B) {"client_id": {"token": "...",        → 完整 meta
                        "tier": "public"|"private",
                        "rate_limit_per_min": int,
                        "rate_limit_per_day": int}}

    返回 meta: {client_id, tier, rate_limit_per_min, rate_limit_per_day}
    """
    out: dict[str, dict] = {}
    try:
        with open(API_KEYS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning("Failed to load API keys from %s: %s", API_KEYS_FILE, e)
        return out

    for client_id, value in data.items():
        # 跳过以 _ 开头的元数据字段（如 _comment）
        if client_id.startswith("_"):
            continue
        if isinstance(value, str) and value:
            # 旧格式：client_id → raw token，默认 private
            out[value] = {
                "client_id": client_id,
                "tier": "private",
                "rate_limit_per_min": 0,
                "rate_limit_per_day": 0,
            }
        elif isinstance(value, dict):
            tok = value.get("token", "")
            if isinstance(tok, str) and tok:
                out[tok] = {
                    "client_id": client_id,
                    "tier": value.get("tier", "private"),
                    "rate_limit_per_min": int(value.get("rate_limit_per_min", 0)),
                    "rate_limit_per_day": int(value.get("rate_limit_per_day", 0)),
                }
    return out


# ============================================================
# 按 IP 限流（tier=public 才生效）
# ============================================================

# {ip: deque[float]} — 滑动窗口时间戳
_rate_window_min: dict[str, deque] = defaultdict(deque)
_rate_window_day: dict[str, deque] = defaultdict(deque)


def _check_rate_limit(ip: str, per_min: int, per_day: int) -> tuple[bool, int]:
    """返回 (是否通过, retry_after_seconds)。

    - per_min=0 且 per_day=0 → 无限流
    - 超过窗口 → 不通过，retry_after=窗口剩余时间
    - 通过则同时记入两个窗口
    """
    if per_min <= 0 and per_day <= 0:
        return True, 0
    now = time.time()
    # 清过期
    wmin = _rate_window_min[ip]
    wday = _rate_window_day[ip]
    while wmin and now - wmin[0] > 60:
        wmin.popleft()
    while wday and now - wday[0] > 86400:
        wday.popleft()
    # 命中限流
    if per_min and len(wmin) >= per_min:
        return False, max(1, int(60 - (now - wmin[0])))
    if per_day and len(wday) >= per_day:
        return False, max(1, int(86400 - (now - wday[0])))
    # 通过 + 记入
    wmin.append(now)
    wday.append(now)
    return True, 0


# 兼容旧用法：保留 API_KEYS 反向索引（client_id → token 集合）
API_KEYS: dict[str, dict] = load_api_keys()

# 开了强制认证却没有可用 token → 谁也进不来（本地自用场景常见）。
# 这里自动降级为软认证，日志里明确提示，避免装完就打不开。
if API_ENFORCE and not API_KEYS:
    logger.warning(
        "LAW_API_ENFORCE=1 但未读到任何 token（%s 不存在或为空）→ 自动降级为免认证模式",
        API_KEYS_FILE,
    )
    API_ENFORCE = False

logger.info(
    "API keys loaded: %d client(s)=%s; enforce=%s",
    len(API_KEYS),
    sorted({m["client_id"] for m in API_KEYS.values()}),
    API_ENFORCE,
)
PUBLIC_CLIENTS = [m["client_id"] for m in API_KEYS.values() if m["tier"] == "public"]
if PUBLIC_CLIENTS:
    logger.info("Public-tier clients (IP rate limited): %s", PUBLIC_CLIENTS)


def _extract_bearer(authorization: str | None) -> str:
    if not authorization:
        return ""
    s = authorization.strip()
    return s[7:] if s.lower().startswith("bearer ") else ""


def verify_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> str:
    """FastAPI 依赖：从 Authorization 头校验 token，返回 client_id。

    - 强制模式：未带/带错 token → 401
    - 软认证模式（LAW_API_ENFORCE=0）：未带 token → "unknown"；带错 token → 401
    - tier=public 的 client 命中 IP 限流 → 429 + Retry-After
    """
    token = _extract_bearer(authorization)
    if token and token in API_KEYS:
        meta = API_KEYS[token]
        # 公开 tier 走 IP 限流
        if meta["tier"] == "public":
            ip = request.client.host if request.client else "unknown"
            ok, retry = _check_rate_limit(
                ip, meta["rate_limit_per_min"], meta["rate_limit_per_day"]
            )
            if not ok:
                logger.info(
                    "Rate-limited: client=%s ip=%s retry_after=%ds",
                    meta["client_id"], ip, retry,
                )
                raise HTTPException(
                    status_code=429,
                    detail=f"调用频次超限（每 IP {meta['rate_limit_per_min']}/min, "
                           f"{meta['rate_limit_per_day']}/day），请 {retry}s 后重试",
                    headers={"Retry-After": str(retry)},
                )
        return meta["client_id"]

    if API_ENFORCE or token:
        # 强制模式，或软认证下带了错 token（避免错 token 被静默放过）
        raise HTTPException(status_code=401, detail="缺少或无效的 API Token")
    # 软认证 + 未带 token
    return "unknown"


def log_call(
    *,
    client_id: str,
    endpoint: str,
    query: Any,
    result_count: int,
    elapsed_ms: int,
    status: int = 200,
) -> None:
    """追加一条结构化调用日志（JSONL）。失败仅记录 warning，不抛异常。"""
    entry = {
        "ts": datetime.datetime.utcnow().isoformat() + "Z",
        "client_id": client_id,
        "endpoint": endpoint,
        "query": (str(query)[:200] if query is not None else ""),
        "result_count": int(result_count),
        "elapsed_ms": int(elapsed_ms),
        "status": int(status),
        "ip": "",  # 由 uvicorn access log 记录，此处不重复
    }
    try:
        with open(CALL_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning("Failed to write call log: %s", e)


# ============================================================
# 中文数字转换
# ============================================================

_CN_DIGIT_MAP = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                  "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT_MAP = {"十": 10, "百": 100, "千": 1000, "万": 10000}


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
    text = text.strip()
    if text.isdigit():
        return int(text)
    match = re.search(r"第?([\u4e00-\u9fa5]+)条?", text)
    if match:
        return chinese_to_arabic(match.group(1))
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
    """标准化法律名称，去除书名号。"""
    return name.strip().replace("《", "").replace("》", "")


# ============================================================
# SQLite 数据库
# ============================================================

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


db = get_db()


def _ensure_db_compatible() -> None:
    """老数据包兼容：补齐新增列（幂等，可反复执行）。

    开源版数据包会持续演进，用户手里的库可能是旧 schema。
    这里在启动时自动补列，避免升级后直接报 "no such column"。
    """
    try:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "law_articles" not in tables:
            logger.warning("law_articles 表不存在（数据包未放入？）：%s", DB_PATH)
            return
        try:
            sys.path.insert(0, str(_BASE_DIR / "ingest"))
            from schema import ensure_schema  # noqa: PLC0415

            ensure_schema(db)
            logger.info("schema 检查完成（ingest/schema.py）")
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("ingest/schema.py 不可用，改用内置迁移：%s", e)
        existing = {r[1] for r in db.execute("PRAGMA table_info(law_articles)")}
        for column, ddl in {
            "version_id": "INTEGER",
            "article_suffix": "TEXT DEFAULT ''",
            "content_hash": "TEXT",
            "repeal_date": "TEXT",
            "repeal_reason": "TEXT",
            "replaced_by": "TEXT",
            "is_principle_clause": "INTEGER DEFAULT 0",
        }.items():
            if column not in existing:
                db.execute(f"ALTER TABLE law_articles ADD COLUMN {column} {ddl}")
        db.commit()
        logger.info("schema 已补齐（内置迁移）")
    except Exception as e:  # noqa: BLE001
        logger.warning("schema 兼容检查失败：%s", e)


_ensure_db_compatible()


# ============================================================
# ChromaDB 向量库（懒加载）
# ============================================================

_chroma_collection = None
_embedding_model = None
_chroma_init_attempted = False
_chroma_available = False


def init_chromadb():
    """懒加载 ChromaDB 和 embedding 模型。只在第一次搜索时初始化。"""
    global _chroma_collection, _embedding_model, _chroma_init_attempted, _chroma_available

    if _chroma_init_attempted:
        return

    _chroma_init_attempted = True

    if not os.path.exists(CHROMA_PATH):
        logger.warning("ChromaDB path not found: %s", CHROMA_PATH)
        return

    try:
        import chromadb
        from sentence_transformers import SentenceTransformer
        import torch

        device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info("Initializing ChromaDB (device=%s)...", device)

        client = chromadb.PersistentClient(path=CHROMA_PATH)
        _chroma_collection = client.get_collection(name=COLLECTION_NAME)

        _embedding_model = SentenceTransformer(EMBEDDING_MODEL, device=device)

        count = _chroma_collection.count()
        _chroma_available = True
        logger.info("ChromaDB ready: %d records, model=%s", count, EMBEDDING_MODEL)

    except Exception as e:
        logger.warning("ChromaDB initialization failed: %s (falling back to SQLite FTS)", e)
        _chroma_available = False


# ============================================================
# FastAPI 应用
# ============================================================

app = FastAPI(title="China Law Search API", version="2.2.0")


@app.get("/health")
async def health():
    """健康检查 — 返回双库状态。免认证。"""
    result: dict[str, Any] = {
        "status": "ok",
        "version": "2.2.0",
        "api_enforce": API_ENFORCE,
        "clients": sorted({m["client_id"] for m in API_KEYS.values()}),
        "public_clients": PUBLIC_CLIENTS,
    }

    # SQLite 状态（count=含历史版本，effective=现行有效）
    try:
        sqlite_count = db.execute("SELECT COUNT(*) FROM law_articles").fetchone()[0]
        effective_count = db.execute(
            "SELECT COUNT(*) FROM law_articles WHERE status IS NULL OR status != 'repealed'"
        ).fetchone()[0]
        result["sqlite"] = {"status": "ok", "count": sqlite_count, "effective": effective_count}
    except Exception as e:
        result["sqlite"] = {"status": "error", "message": str(e)}
        result["status"] = "degraded"

    # ChromaDB 状态
    if _chroma_available and _chroma_collection:
        try:
            chroma_count = _chroma_collection.count()
            result["chromadb"] = {"status": "ok", "count": chroma_count}
        except Exception as e:
            result["chromadb"] = {"status": "error", "message": str(e)}
            result["status"] = "degraded"
    else:
        result["chromadb"] = {"status": "not_initialized"}

    result["embedding_model"] = EMBEDDING_MODEL if _chroma_available else None
    return result


# ============================================================
# API: 语义检索
# ============================================================

@app.get("/api/law/search")
async def search_laws(
    q: str = Query(..., min_length=1),
    k: int = Query(default=10, ge=1, le=50),
    type: str | None = Query(default=None),
    include_history: bool = Query(default=False, description="是否包含已被修订的旧版本条文"),
    client_id: str = Depends(verify_token),
):
    """向量语义检索法条。ChromaDB 不可用时降级为 SQLite FTS 全文搜索。"""
    start = time.time()
    items: list[dict] = []
    engine = "sqlite_fts"

    # 尝试 ChromaDB 向量检索
    if _chroma_available:
        try:
            items = _search_via_chromadb(q, k, type, include_history)
            engine = "chromadb"
        except Exception as e:
            logger.warning("ChromaDB search failed, falling back to SQLite: %s", e)

    if not items and engine != "chromadb":
        items = _search_via_sqlite(q, k, type, include_history)

    # 含历史结果时，现行有效条文一律排在已失效条文之前（stable，不打乱相关性顺序）
    if include_history:
        items.sort(key=lambda x: x.get("is_repealed", False))

    elapsed_ms = int((time.time() - start) * 1000)
    log_call(
        client_id=client_id,
        endpoint="/api/law/search",
        query=q,
        result_count=len(items),
        elapsed_ms=elapsed_ms,
    )
    return {
        "items": items,
        "total": len(items),
        "query": q,
        "elapsed_ms": elapsed_ms,
        "engine": engine,
    }


def _search_via_chromadb(q: str, k: int, law_type: str | None,
                        include_history: bool = False) -> list[dict]:
    """使用 ChromaDB 向量检索。旧版本条文默认过滤。"""
    # 生成查询向量
    query_embedding = _embedding_model.encode(
        [q], normalize_embeddings=True,
    )

    # 构建 filter：状态不在向量库层过滤（旧集合可能没有 status 元数据，
    # 加了 where 条件会直接查空），改为取回后按元数据在内存里筛。
    where_filter = {"law_type": law_type} if law_type else None

    results = _chroma_collection.query(
        query_embeddings=query_embedding.tolist(),
        n_results=k,
        where=where_filter,
    )

    items = []
    for meta, dist in zip(results["metadatas"][0], results["distances"][0]):
        status = meta.get("status") or "effective"
        if status == "repealed" and not include_history:
            continue
        items.append({
            "law_name": meta.get("law_name", ""),
            "law_type": meta.get("law_type", ""),
            "law_type_label": meta.get("law_type_label", ""),
            "article_number": meta.get("article_number", ""),
            "article_num": meta.get("article_number_num", 0),
            "article_suffix": meta.get("article_suffix") or "",
            "chapter": meta.get("chapter", ""),
            "section": meta.get("section", ""),
            "content": meta.get("content", ""),
            "publish_date": meta.get("publish_date", ""),
            "effective_date": meta.get("effective_date", ""),
            "status": status,
            "is_repealed": status == "repealed",
            "repeal_date": meta.get("repeal_date") or "",
            "replaced_by": meta.get("replaced_by") or "",
            "score": round(1 - dist, 4),
        })
    return items


def _row_to_item(r, score: float = 0.0) -> dict:
    """把一行法条记录转成 API 返回结构（含时效状态，供前端标注旧版本）。"""
    status = r["status"] or "effective"
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
        "effective_date": r["effective_date"] or "",
        "status": status,
        "is_repealed": status == "repealed",
        "repeal_date": r["repeal_date"] or "",
        "replaced_by": r["replaced_by"] or "",
        "score": score,
    }


_COLS = """a.law_name, a.law_type, a.law_type_label, a.article_number,
           a.article_number_num, a.article_suffix, a.chapter, a.section, a.content,
           a.publish_date, a.effective_date, a.status, a.repeal_date, a.replaced_by"""


def _fts_query(q: str) -> str | None:
    """把用户输入转成 FTS5 trigram 查询串。

    trigram 分词器按 3 字滑动窗口切词，短于 3 字的查询无法命中，
    且未加引号时特殊符号会被当成语法 → 统一加双引号并转义。
    """
    cleaned = q.strip().replace('"', '""')
    if len(cleaned) < 3:
        return None
    return f'"{cleaned}"'


def _match_terms(q: str, size: int = 2, limit: int = 8) -> list[str]:
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


def _search_via_sqlite(q: str, k: int, law_type: str | None,
                       include_history: bool = False) -> list[dict]:
    """SQLite 全文检索：优先 FTS5(trigram) + bm25 排序，失败或零命中再退化 LIKE。"""
    type_filter = ""
    params: list[Any] = []
    if law_type:
        type_filter += " AND a.law_type = ?"
        params.append(law_type)
    if not include_history:
        type_filter += " AND (a.status IS NULL OR a.status != 'repealed')"

    # 1) FTS5 检索（有 bm25 相关性排序）
    fts = _fts_query(q)
    if fts:
        try:
            rows = db.execute(
                f"""SELECT {_COLS}, bm25(law_articles_fts) AS rank
                    FROM law_articles_fts
                    JOIN law_articles a ON a.id = law_articles_fts.rowid
                    WHERE law_articles_fts MATCH ?{type_filter}
                    ORDER BY rank
                    LIMIT ?""",
                (fts, *params, k),
            ).fetchall()
            if rows:
                # bm25 越小越相关，转成正向 0~1 分数便于前端展示
                return [_row_to_item(r, round(1.0 / (1.0 + abs(r["rank"])), 4)) for r in rows]
        except Exception as e:  # noqa: BLE001
            logger.warning("FTS 检索失败，改用 LIKE：%s", e)

    # 2) LIKE 兜底（短查询、FTS 未建、零命中）
    rows = db.execute(
        f"""SELECT {_COLS}
            FROM law_articles a
            WHERE (a.content LIKE ? OR a.article LIKE ?){type_filter}
            LIMIT ?""",
        (f"%{q}%", f"%{q}%", *params, k),
    ).fetchall()
    if rows:
        return [_row_to_item(r) for r in rows]

    # 3) 片段兜底：中文没有空格，用户写「二倍工资」而原文是「二倍的工资」时，
    #    字面连续匹配必然落空。这里拆成 2 字片段，命中多数片段即召回，
    #    按命中数排序——recision 让一点，换可用性。
    terms = _match_terms(q)
    if terms:
        n = len(terms)
        need = max(2, math.ceil(n * 0.6)) if n > 1 else 1
        score_sql = " + ".join("(a.content LIKE ?)" for _ in terms)
        rows = db.execute(
            f"""SELECT * FROM (
                    SELECT {_COLS}, {score_sql} AS hits
                    FROM law_articles a
                    WHERE 1 = 1{type_filter}
                ) WHERE hits >= ?
                ORDER BY hits DESC, LENGTH(content) ASC
                LIMIT ?""",
            (*[f"%{x}%" for x in terms], *params, need, k),
        ).fetchall()
        return [_row_to_item(r, round(r["hits"] / n, 4)) for r in rows]

    return []

# ============================================================
# API: 精确条号查找
# ============================================================

@app.get("/api/law/article")
async def get_law_article(
    law_name: str = Query(..., min_length=1),
    article_num: int = Query(..., ge=1),
    article_suffix: str | None = Query(
        default=None, description="增补条后缀，如「之一」（刑法第一百三十三条之一）"),
    include_history: bool = Query(default=False, description="是否返回已被修订的旧版本条文"),
    client_id: str = Depends(verify_token),
):
    """根据法律名称和条号获取法条正文。支持模糊匹配法律名称。"""
    start = time.time()
    normalized_name = normalize_law_name(law_name)
    original_name = law_name.strip()
    history_filter = "" if include_history else " AND (status IS NULL OR status != 'repealed')"
    # 没指定后缀时只认正条，避免「第八十二条」把「第八十二条之一」串进来
    suffix_filter = " AND article_suffix = ?" if article_suffix else \
        " AND (article_suffix = '' OR article_suffix IS NULL)"
    suffix_args: tuple = (article_suffix,) if article_suffix else ()
    logger.info("Looking up: law_name=%s (normalized=%s), article_num=%d",
                law_name, normalized_name, article_num)

    # 精确匹配
    row = None
    for name_to_try in [original_name, normalized_name]:
        row = db.execute(
            f"""SELECT law_name, law_type, law_type_label, article_number, article_number_num,
                      article_suffix, chapter, section, article, content, publish_date, effective_date,
                      status, repeal_date, repeal_reason, replaced_by, is_principle_clause,
                      source_file
               FROM law_articles
               WHERE law_name = ? AND article_number_num = ?{history_filter}{suffix_filter}
               ORDER BY CASE WHEN status = 'repealed' THEN 1 ELSE 0 END, id DESC
               LIMIT 1""",
            (name_to_try, article_num, *suffix_args),
        ).fetchone()
        if row:
            break

    # 模糊匹配
    if not row:
        rows = db.execute(
            f"""SELECT law_name, law_type, law_type_label, article_number, article_number_num,
                      article_suffix, chapter, section, article, content, publish_date, effective_date,
                      status, repeal_date, repeal_reason, replaced_by, is_principle_clause,
                      source_file
               FROM law_articles
               WHERE (law_name LIKE ? OR law_name LIKE ?) AND article_number_num = ?{history_filter}{suffix_filter}
               ORDER BY CASE WHEN status = 'repealed' THEN 1 ELSE 0 END, LENGTH(law_name) ASC
               LIMIT 5""",
            (f"%{normalized_name}%", f"%{original_name}%", article_num, *suffix_args),
        ).fetchall()
        exact_rows = [r for r in rows if "解释" not in r["law_name"]]
        row = exact_rows[0] if exact_rows else (rows[0] if rows else None)

    elapsed_ms = int((time.time() - start) * 1000)
    if not row:
        log_call(
            client_id=client_id,
            endpoint="/api/law/article",
            query=f"{law_name}#{article_num}",
            result_count=0,
            elapsed_ms=elapsed_ms,
        )
        logger.warning("Not found: law_name=%s, article_num=%d", normalized_name, article_num)
        return {"error": "not_found", "law_name": law_name, "article_num": article_num}

    result = {
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
    log_call(
        client_id=client_id,
        endpoint="/api/law/article",
        query=f"{law_name}#{article_num}",
        result_count=1,
        elapsed_ms=elapsed_ms,
    )
    logger.info("Found: law_name=%s, article=%s, content_length=%d",
                row["law_name"], row["article_number"], len(row["content"] or ""))
    return result


# ============================================================
# API: 法条批量校验
# ============================================================

class CitationItem(BaseModel):
    law_name: str
    article_number: str


class VerifyRequest(BaseModel):
    """支持两种 citations 格式：
    1. 字符串列表: ["《民法典》第六百八十条", ...]
    2. 对象列表: [{"law_name": "民法典", "article_number": "第六百八十条"}, ...]
    """
    citations: list[Any]
    case_category: str | None = None


@app.post("/api/law/verify")
async def verify_law_citations(
    payload: VerifyRequest,
    include_history: bool = Query(default=False, description="是否用旧版本条文参与核验"),
    client_id: str = Depends(verify_token),
):
    """法条批量校验 — 返回完整校验状态（存在性、法律状态、废止信息、原则性条款、多版本歧义）。"""
    start = time.time()
    results = []
    history_filter = "" if include_history else " AND (status IS NULL OR status != 'repealed')"

    for citation in payload.citations:
        # 解析引用
        if isinstance(citation, dict):
            law_name = citation.get("law_name", "").strip()
            article_number_str = citation.get("article_number", "").strip()
            article_num = parse_article_number(article_number_str)
            article_suffix = (citation.get("article_suffix") or "").strip() \
                or parse_article_suffix(article_number_str)
        elif isinstance(citation, str):
            parsed = _parse_citation_string(citation)
            law_name = parsed["law_name"]
            article_num = parsed["article_num"]
            article_number_str = parsed.get("article_number", "")
            article_suffix = parsed.get("article_suffix", "")
        else:
            results.append(_build_verify_result(
                law_name=str(citation), article_number="",
                verified=False, error="invalid_citation_format",
            ))
            continue

        if not law_name or article_num <= 0:
            results.append(_build_verify_result(
                law_name=law_name, article_number=article_number_str,
                verified=False, error="parse_failed",
            ))
            continue

        # SQLite 查找（返回所有匹配行，检测多版本歧义）
        normalized = normalize_law_name(law_name)
        suffix_filter = " AND article_suffix = ?" if article_suffix else \
            " AND (article_suffix = '' OR article_suffix IS NULL)"
        suffix_args: tuple = (article_suffix,) if article_suffix else ()
        rows = db.execute(
            f"""SELECT law_name, article_number, article_number_num, article_suffix,
                      version_id, content, publish_date, effective_date, status,
                      repeal_date, repeal_reason, replaced_by, is_principle_clause
               FROM law_articles
               WHERE (law_name LIKE ? OR law_name LIKE ?) AND article_number_num = ?{history_filter}{suffix_filter}
               ORDER BY CASE WHEN law_name = ? OR law_name = ? THEN 0 ELSE 1 END,
                        LENGTH(law_name) ASC""",
            (f"%{normalized}%", f"%{law_name}%", article_num, *suffix_args,
             normalized, law_name),
        ).fetchall()

        if not rows:
            results.append(_build_verify_result(
                law_name=law_name, article_number=article_number_str,
                verified=False, error="未找到该法条",
            ))
            continue

        # 过滤司法解释优先匹配非解释的
        non_interpretation = [r for r in rows if "解释" not in r["law_name"]]
        selected_rows = non_interpretation if non_interpretation else rows

        # 检测多版本歧义：只看同一部法规（同名）是否还有别的版本记录
        row = selected_rows[0]
        same_law = [r for r in selected_rows if r["law_name"] == row["law_name"]]
        versions = {(r["version_id"], r["publish_date"] or "") for r in same_law}
        has_multiple_versions = len(versions) > 1
        results.append(_build_verify_result(
            law_name=row["law_name"],
            article_number=row["article_number"],
            article_suffix=row["article_suffix"] or "",
            verified=True,
            actual_content=row["content"],
            status=row["status"] or "effective",
            effective_date=row["effective_date"] or "",
            repeal_date=row["repeal_date"] or "",
            repeal_reason=row["repeal_reason"] or "",
            replaced_by=row["replaced_by"] or "",
            is_principle_clause=bool(row["is_principle_clause"]),
            has_multiple_versions=has_multiple_versions,
        ))

    elapsed_ms = int((time.time() - start) * 1000)
    log_call(
        client_id=client_id,
        endpoint="/api/law/verify",
        query=f"{len(payload.citations)}条",
        result_count=len(results),
        elapsed_ms=elapsed_ms,
    )
    return {
        "results": results,
        "total": len(results),
        "verified_count": sum(1 for r in results if r.get("verified")),
        "failed_count": sum(1 for r in results if not r.get("verified")),
    }


def _parse_citation_string(citation: str) -> dict:
    """解析字符串引用。

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
) -> dict:
    """构建校验结果，确保所有字段都返回。"""
    return {
        "law_name": law_name,
        "article_number": article_number,
        "article_suffix": article_suffix,
        "verified": verified,
        "actual_content": actual_content,
        "error": error,
        "status": status,
        "effective_date": effective_date,
        "repeal_date": repeal_date,
        "repeal_reason": repeal_reason,
        "replaced_by": replaced_by,
        "is_principle_clause": is_principle_clause,
        "has_multiple_versions": has_multiple_versions,
    }


# ============================================================
# API: 库统计
# ============================================================

@app.get("/api/law/stats")
async def law_stats(client_id: str = Depends(verify_token)):
    """返回法规库统计信息。"""
    start = time.time()
    # SQLite 统计
    try:
        total = db.execute("SELECT COUNT(*) FROM law_articles").fetchone()[0]
        type_rows = db.execute(
            """SELECT law_type, law_type_label, COUNT(*) as count
               FROM law_articles
               GROUP BY law_type, law_type_label
               ORDER BY count DESC"""
        ).fetchall()

        by_type = {}
        for r in type_rows:
            by_type[r["law_type"]] = {
                "count": r["count"],
                "label": r["law_type_label"] or r["law_type"],
            }
    except Exception as e:
        log_call(
            client_id=client_id,
            endpoint="/api/law/stats",
            query="",
            result_count=0,
            elapsed_ms=int((time.time() - start) * 1000),
            status=500,
        )
        return {"error": str(e)}

    # ChromaDB 统计
    chromadb_status = "not_initialized"
    chromadb_count = 0
    if _chroma_available and _chroma_collection:
        try:
            chromadb_count = _chroma_collection.count()
            chromadb_status = "ok"
        except Exception:
            chromadb_status = "error"

    elapsed_ms = int((time.time() - start) * 1000)
    log_call(
        client_id=client_id,
        endpoint="/api/law/stats",
        query="",
        result_count=1,
        elapsed_ms=elapsed_ms,
    )
    effective = db.execute(
        "SELECT COUNT(*) FROM law_articles WHERE status IS NULL OR status != 'repealed'"
    ).fetchone()[0]
    return {
        "total_articles": total,
        "effective_articles": effective,
        "by_type": by_type,
        "chromadb_status": chromadb_status,
        "chromadb_count": chromadb_count,
        "sqlite_status": "ok",
        "embedding_model": EMBEDDING_MODEL if _chroma_available else None,
    }


# ============================================================
# 启动事件
# ============================================================

@app.on_event("startup")
async def startup_event():
    """服务启动时初始化 ChromaDB（如果可用）。"""
    logger.info("Law Search Service v2.1 starting...")
    logger.info("SQLite: %s", DB_PATH)
    logger.info("ChromaDB: %s", CHROMA_PATH)
    logger.info("API keys: %d loaded, enforce=%s", len(API_KEYS), API_ENFORCE)
    logger.info("Call log: %s", CALL_LOG_PATH)

    # 尝试初始化 ChromaDB
    init_chromadb()

    if _chroma_available:
        logger.info("Vector search enabled (ChromaDB)")
    else:
        logger.info("Vector search disabled, using SQLite FTS fallback")


# ============================================================
# 本地查询界面（律师直接打开 http://127.0.0.1:8001 使用）
# ============================================================

_WEB_DIR = _BASE_DIR / "web"
if _WEB_DIR.is_dir():
    from fastapi.staticfiles import StaticFiles

    app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("LAW_HOST", "0.0.0.0"), port=int(os.environ.get("LAW_PORT", "8001")))