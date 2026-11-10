"""fmzy 法条查询服务 — 双库架构（ChromaDB 向量检索 + SQLite 精确查找）。

端口: 8001
数据库:
  - ChromaDB: /home/fumin/lawyer_model/legal_vector_db (向量语义检索，软链接)
  - SQLite:   /home/fumin/law_ingest/law_data.db (精确查找 + 法条校验)
端点:
  GET  /api/law/search?q=X&k=Y&type=Z         向量语义检索（降级为 SQLite FTS）
  GET  /api/law/article?law_name=X&article_num=Y  精确条号查找
  POST /api/law/verify                         法条批量校验
  GET  /api/law/stats                          库统计信息
  GET  /api/health                             健康检查（免认证）

认证（v2.2 起强制）：
  - 每渠道独立 Bearer Token，配置于 /home/fumin/law_api_keys.json
  - 启动时读入；token 校验失败 → 401
  - 环境变量 LAW_API_ENFORCE=0 可临时切回软认证（带 token 记来源，不带 token 放行记 unknown）
  - /api/health 始终免认证（监控用）

渠道分级（v2.2 新增）：
  - tier=private（默认）：不限流
  - tier=public：按 IP 限流（rate_limit_per_min + rate_limit_per_day），超过 429+Retry-After
  - 公网分发版律师团用 public 桶（明显标记的 token + 限流），私域版用 private 桶

统计：
  - 每次调用写一条 JSONL 至 /home/fumin/law_call_log.jsonl
  - 字段：ts, client_id, endpoint, query, result_count, elapsed_ms, status, ip, ua
"""

import datetime
import json
import logging
import os
import re
import sqlite3
import time
from collections import defaultdict, deque
from typing import Any

# 离线模式：fmzy 服务器无外网，模型已缓存在本地
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request

from pydantic import BaseModel

import law_state  # 条文状态唯一实现，与 china-law-search-api/server/law_state.py 同源

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("law_search")

# ============================================================
# 配置
# ============================================================

DB_PATH = "/home/fumin/law_ingest/law_data_20261108.db"
CHROMA_PATH = "/home/fumin/lawyer_model/legal_vector_db"
COLLECTION_NAME = "chinese_laws"
# 增量补缺集合（独立路径，绝不写入现有损坏库）
GAP_CHROMA_PATH = "/home/fumin/law_ingest/legal_vector_db_gap"
GAP_COLLECTION_NAME = "chinese_laws_gap"
GJGZK_CHROMA_PATH = "/home/fumin/law_ingest/legal_vector_db_gjgzk"
GJGZK_COLLECTION_NAME = "chinese_laws_gjgzk"
EMBEDDING_MODEL = "BAAI/bge-large-zh-v1.5"

# 认证与统计
API_KEYS_FILE = "/home/fumin/law_api_keys.json"
CALL_LOG_PATH = "/home/fumin/law_call_log.jsonl"
# 强制模式：1=无 token/错 token → 401；0=软认证（仅记来源，不挡）
API_ENFORCE = os.environ.get("LAW_API_ENFORCE", "1") not in ("0", "false", "False", "")


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


# ============================================================
# ChromaDB 向量库（懒加载）
# ============================================================

_chroma_collection = None
_chroma_gap_collection = None
_chroma_gjgzk_collection = None
_embedding_model = None
_chroma_init_attempted = False
_chroma_available = False


def init_chromadb():
    """懒加载 ChromaDB 和 embedding 模型。只在第一次搜索时初始化。"""
    global _chroma_collection, _chroma_gap_collection, _chroma_gjgzk_collection, _embedding_model, _chroma_init_attempted, _chroma_available

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

        # 增量补缺集合：独立路径，避免写入现有库
        gap_count = 0
        if os.path.exists(GAP_CHROMA_PATH):
            try:
                gap_client = chromadb.PersistentClient(path=GAP_CHROMA_PATH)
                _chroma_gap_collection = gap_client.get_collection(name=GAP_COLLECTION_NAME)
                gap_count = _chroma_gap_collection.count()
            except Exception as gap_err:
                logger.warning("Gap collection unavailable: %s", gap_err)
                _chroma_gap_collection = None

        gjgzk_count = 0
        if os.path.exists(GJGZK_CHROMA_PATH):
            try:
                gjgzk_client = chromadb.PersistentClient(path=GJGZK_CHROMA_PATH)
                _chroma_gjgzk_collection = gjgzk_client.get_collection(name=GJGZK_COLLECTION_NAME)
                gjgzk_count = _chroma_gjgzk_collection.count()
            except Exception as gjgzk_err:
                logger.warning("GJGZK collection unavailable: %s", gjgzk_err)
                _chroma_gjgzk_collection = None

        _embedding_model = SentenceTransformer(EMBEDDING_MODEL, device=device)

        count = _chroma_collection.count()
        _chroma_available = True
        logger.info("ChromaDB ready: %d records (+gap %d, +gjgzk %d), model=%s",
                    count, gap_count, gjgzk_count, EMBEDDING_MODEL)

    except Exception as e:
        logger.warning("ChromaDB initialization failed: %s (falling back to SQLite FTS)", e)
        _chroma_available = False


# ============================================================
# FastAPI 应用
# ============================================================

app = FastAPI(title="FMZY Law Search", version="2.1.0")


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

    # SQLite 状态
    try:
        sqlite_count = db.execute("SELECT COUNT(*) FROM law_articles").fetchone()[0]
        result["sqlite"] = {"status": "ok", "count": sqlite_count}
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

    if _chroma_gap_collection is not None:
        try:
            result["chromadb_gap"] = {"status": "ok", "count": _chroma_gap_collection.count()}
        except Exception as e:
            result["chromadb_gap"] = {"status": "error", "message": str(e)}
    else:
        result["chromadb_gap"] = {"status": "not_initialized"}

    if _chroma_gjgzk_collection is not None:
        try:
            result["chromadb_gjgzk"] = {"status": "ok", "count": _chroma_gjgzk_collection.count()}
        except Exception as e:
            result["chromadb_gjgzk"] = {"status": "error", "message": str(e)}
    else:
        result["chromadb_gjgzk"] = {"status": "not_initialized"}

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
    client_id: str = Depends(verify_token),
):
    """向量语义检索法条。ChromaDB 不可用时降级为 SQLite FTS 全文搜索。"""
    start = time.time()
    items: list[dict] = []
    engine = "sqlite_fts"

    # 尝试 ChromaDB 向量检索
    if _chroma_available:
        try:
            items = _search_via_chromadb(q, k, type)
            items = _keep_current_articles(items)
            if len(items) < k:
                items = _merge_sqlite_items(items, q, k, type)
            engine = "chromadb+sqlite"
        except Exception as e:
            logger.warning("ChromaDB search failed, falling back to SQLite: %s", e)

    if not items and engine != "chromadb":
        items = _search_via_sqlite(q, k, type)

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


def _search_via_chromadb(q: str, k: int, law_type: str | None) -> list[dict]:
    """使用 ChromaDB 向量检索；主集合 + 增量补缺集合合并去重。"""
    # 生成查询向量
    query_embedding = _embedding_model.encode(
        [q], normalize_embeddings=True,
    )

    # 构建 filter
    where_filter = None
    if law_type:
        where_filter = {"law_type": law_type}

    def _query_one(coll) -> list[dict]:
        results = coll.query(
            query_embeddings=query_embedding.tolist(),
            n_results=k,
            where=where_filter,
        )
        out = []
        for meta, dist in zip(results["metadatas"][0], results["distances"][0]):
            out.append({
                "law_name": meta.get("law_name", ""),
                "law_type": meta.get("law_type", ""),
                "law_type_label": meta.get("law_type_label", ""),
                "article_number": meta.get("article_number", ""),
                "article_num": meta.get("article_number_num", 0),
                "chapter": meta.get("chapter", ""),
                "section": meta.get("section", ""),
                "content": meta.get("content", ""),
                "publish_date": meta.get("publish_date", ""),
                "effective_date": meta.get("effective_date", ""),
                "score": round(1 - dist, 4),
            })
        return out

    items = _query_one(_chroma_collection)
    if _chroma_gap_collection is not None:
        try:
            items.extend(_query_one(_chroma_gap_collection))
        except Exception as e:
            logger.warning("Gap collection search failed: %s", e)

    if _chroma_gjgzk_collection is not None:
        try:
            items.extend(_query_one(_chroma_gjgzk_collection))
        except Exception as e:
            logger.warning("GJGZK collection search failed: %s", e)

    # 按 (法规名, 条号) 去重，保留得分更高的
    best: dict[tuple[str, str], dict] = {}
    for it in items:
        key = (it["law_name"], it["article_number"])
        if key not in best or it["score"] > best[key]["score"]:
            best[key] = it
    merged = sorted(best.values(), key=lambda x: x["score"], reverse=True)
    return merged[:k]


_ITEM_COLUMNS = ("law_name, law_type, law_type_label, article_number, article_number_num, "
                 "chapter, section, content, publish_date, effective_date")


def _row_to_item(row: Any, score: float) -> dict:
    """把 SQLite 行转成接口字段。"""
    return {
        "law_name": row["law_name"] or "",
        "law_type": row["law_type"] or "",
        "law_type_label": row["law_type_label"] or "",
        "article_number": row["article_number"] or "",
        "article_num": row["article_number_num"] or 0,
        "chapter": row["chapter"] or "",
        "section": row["section"] or "",
        "content": row["content"] or "",
        "publish_date": row["publish_date"] or "",
        "effective_date": row["effective_date"] or "",
        "score": score,
    }


def _keep_current_articles(items: list[dict]) -> list[dict]:
    """按法规名+条号回查现行条文，丢弃废止/已下线结果并覆盖为现行正文。

    向量库是历史快照，document 可能是旧版文本；检索出口统一以 SQLite 为准。
    """
    fresh: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        key = (item.get("law_name", ""), item.get("article_number", ""))
        if not all(key) or key in seen:
            continue
        row = db.execute(
            f"SELECT {_ITEM_COLUMNS} FROM law_articles "
            "WHERE law_name = ? AND article_number = ? AND status = 'effective' "
            "ORDER BY id DESC LIMIT 1", key).fetchone()
        if row is None:
            continue
        seen.add(key)
        fresh.append(_row_to_item(row, item.get("score", 0.0)))
    return fresh


def _merge_sqlite_items(items: list[dict], q: str, k: int,
                        law_type: str | None) -> list[dict]:
    """向量结果不足 k 条时用 FTS 补位，覆盖尚未进入向量库的新收录条文。"""
    seen = {(i.get("law_name", ""), i.get("article_number", "")) for i in items}
    for item in _search_via_sqlite(q, k, law_type):
        key = (item.get("law_name", ""), item.get("article_number", ""))
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
        if len(items) >= k:
            break
    return items


def _search_via_sqlite(q: str, k: int, law_type: str | None) -> list[dict]:
    """使用 SQLite FTS 全文搜索（降级方案）。"""
    type_filter = ""
    params: list[Any] = []
    if law_type:
        type_filter = " AND law_type = ?"
        params.append(law_type)

    # 尝试 FTS 搜索
    try:
        rows = db.execute(
            f"""SELECT law_name, law_type, law_type_label, article_number, article_number_num,
                       chapter, section, content, publish_date, effective_date, source_file
                FROM law_articles
                WHERE id IN (SELECT rowid FROM law_articles_fts WHERE law_articles_fts MATCH ?)
                  AND status = 'effective'
                {type_filter}
                LIMIT ?""",
            (q, *params, k),
        ).fetchall()
    except Exception:
        # FTS 不可用时降级为 LIKE
        rows = db.execute(
            f"""SELECT law_name, law_type, law_type_label, article_number, article_number_num,
                       chapter, section, content, publish_date, effective_date, source_file
                FROM law_articles
                WHERE content LIKE ?
                  AND status = 'effective'
                {type_filter}
                LIMIT ?""",
            (f"%{q}%", *params, k),
        ).fetchall()

    return [
        {
            "law_name": r["law_name"],
            "law_type": r["law_type"],
            "law_type_label": r["law_type_label"],
            "article_number": r["article_number"],
            "article_num": r["article_number_num"],
            "chapter": r["chapter"],
            "section": r["section"],
            "content": r["content"],
            "publish_date": r["publish_date"] or "",
            "effective_date": r["effective_date"] or "",
            "score": 0.0,
        }
        for r in rows
    ]


def _law_name_candidates(law_name: str) -> list[str]:
    """把用户写的法名换成库内全称：先精确，再按包含匹配（跳过司法解释）。"""
    raw = (law_name or "").strip()
    normalized = normalize_law_name(raw)
    names: list[str] = []
    for n in (raw, normalized):
        if n and n not in names:
            names.append(n)
    if not normalized:
        return names
    rows = db.execute(
        """SELECT DISTINCT law_name FROM law_versions
           WHERE law_name LIKE ?
           ORDER BY LENGTH(law_name) ASC LIMIT 8""",
        (f"%{normalized}%",),
    ).fetchall()
    for r in rows:
        name = r["law_name"] or ""
        if not name or "解释" in name or name in names:
            continue
        names.append(name)
    return names


# ============================================================
# API: 精确条号查找
# ============================================================

@app.get("/api/law/article")
async def get_law_article(
    law_name: str = Query(..., min_length=1),
    article_num: int = Query(..., ge=1),
    client_id: str = Depends(verify_token),
):
    """根据法律名称和条号获取法条正文。支持模糊匹配法律名称。"""
    start = time.time()
    normalized_name = normalize_law_name(law_name)
    logger.info("Looking up: law_name=%s (normalized=%s), article_num=%d",
                law_name, normalized_name, article_num)

    # 状态判定统一交给 law_state：版本、条文状态、能否引用一次算清
    item = law_state.resolve_article(
        db, _law_name_candidates(law_name), article_num, "",
        include_history=True,
        article_number_text=f"第{article_num}条",
    )

    elapsed_ms = int((time.time() - start) * 1000)
    if not item or item.get("error") == "not_found":
        log_call(
            client_id=client_id,
            endpoint="/api/law/article",
            query=f"{law_name}#{article_num}",
            result_count=0,
            elapsed_ms=elapsed_ms,
        )
        logger.warning("Not found: law_name=%s, article_num=%d", normalized_name, article_num)
        return {"error": "not_found", "law_name": law_name, "article_num": article_num}

    # law_type / source_file 只在版本表上，补齐给既有调用方
    meta = {}
    version_id = (item.get("version") or {}).get("version_id")
    if version_id:
        mrow = db.execute(
            "SELECT law_type, law_type_label, source_file FROM law_versions WHERE id = ?",
            (version_id,),
        ).fetchone()
        if mrow:
            meta = {
                "law_type": mrow["law_type"] or "",
                "law_type_label": mrow["law_type_label"] or "",
                "source_file": mrow["source_file"] or "",
            }

    result = {
        "law_name": item["law_name"],
        "law_type": meta.get("law_type", ""),
        "law_type_label": meta.get("law_type_label", ""),
        "article_number": item["article_number"],
        "article_num": item["article_num"],
        "chapter": item["chapter"],
        "section": item["section"],
        "article": item["article"],
        "content": item["content"],
        "publish_date": item["publish_date"] or "",
        "effective_date": item["effective_date"] or "",
        "status": item["status"] or "effective",
        "repeal_date": item["repeal_date"] or "",
        "repeal_reason": item["repeal_reason"] or "",
        "replaced_by": item["replaced_by"] or "",
        "is_principle_clause": bool(item["is_principle_clause"]),
        "source_file": meta.get("source_file", ""),
        # 本次统一后的判据：能不能引只看 citation_valid
        "law_status": item["law_status"],
        "article_status": item["article_status"],
        "citation_valid": item["citation_valid"],
        "version": item["version"],
        "message": item.get("message"),
        "previous_version": item.get("previous_version"),
        "previous_content": item.get("previous_content"),
        "previous_article_number": item.get("previous_article_number"),
        "renumber_candidates": item.get("renumber_candidates") or [],
    }
    log_call(
        client_id=client_id,
        endpoint="/api/law/article",
        query=f"{law_name}#{article_num}",
        result_count=1,
        elapsed_ms=elapsed_ms,
    )
    logger.info("Found: law_name=%s, article=%s, article_status=%s, citation_valid=%s",
                item["law_name"], item["article_number"], item["article_status"],
                item["citation_valid"])
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
async def verify_law_citations(payload: VerifyRequest, client_id: str = Depends(verify_token)):
    """法条批量校验 — 返回完整校验状态（存在性、法律状态、废止信息、原则性条款、多版本歧义）。"""
    start = time.time()
    results = []

    for citation in payload.citations:
        # 解析引用
        if isinstance(citation, dict):
            law_name = citation.get("law_name", "").strip()
            article_number_str = citation.get("article_number", "").strip()
            article_num = parse_article_number(article_number_str)
        elif isinstance(citation, str):
            parsed = _parse_citation_string(citation)
            law_name = parsed["law_name"]
            article_num = parsed["article_num"]
            article_number_str = parsed.get("article_number", "")
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

        # 状态判定统一交给 law_state，与 /api/law/article 共用同一套规则
        item = law_state.resolve_article(
            db, _law_name_candidates(law_name), article_num, "",
            include_history=False,
            article_number_text=article_number_str or f"第{article_num}条",
        )
        if item is None or item.get("error") == "not_found":
            results.append(_build_verify_result(
                law_name=(item or {}).get("law_name") or law_name,
                article_number=article_number_str,
                verified=False,
                error=(item or {}).get("message") or "未找到该法条",
                law_status=(item or {}).get("law_status"),
                version=(item or {}).get("version"),
            ))
            continue

        article_status = item.get("article_status")
        common = {
            "law_name": item["law_name"],
            "article_number": item["article_number"],
            "article_suffix": item["article_suffix"],
            "status": item["status"],
            "effective_date": item["effective_date"],
            "repeal_date": item["repeal_date"],
            "repeal_reason": item["repeal_reason"],
            "replaced_by": item["replaced_by"],
            "is_principle_clause": item["is_principle_clause"],
            "has_multiple_versions": item["has_multiple_versions"],
            "law_status": item["law_status"],
            "article_status": article_status,
            "citation_valid": item["citation_valid"],
            "version": item["version"],
            "message": item.get("message"),
            "previous_version": item.get("previous_version"),
            "previous_content": item.get("previous_content"),
            "previous_article_number": item.get("previous_article_number"),
            "renumber_candidates": item.get("renumber_candidates"),
        }

        if article_status == law_state.ARTICLE_IN_FORCE:
            results.append(_build_verify_result(
                verified=True, actual_content=item["content"], **common,
            ))
            continue

        errors = {
            law_state.ARTICLE_REMOVED: "现行版已删除该条",
            law_state.ARTICLE_RENUMBERED: "该条号已在现行版调整",
            law_state.ARTICLE_REPEALED: (
                "该法规已废止"
                if item["law_status"] == law_state.LAW_REPEALED else "该条文已废止"
            ),
            law_state.ARTICLE_UNKNOWN: "未收录现行版，无法确认",
        }
        results.append(_build_verify_result(
            verified=False, error=errors.get(article_status, "未找到该法条"), **common,
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
    """解析字符串引用格式: "《法律名称》第X条" 或 "法律名称第X条"。"""
    name_match = re.match(r"[《]?(.+?)[》]?第", citation)
    article_match = re.search(r"第([\u4e00-\u9fa5]+)条", citation)

    if name_match and article_match:
        return {
            "law_name": name_match.group(1).strip(),
            "article_number": f"第{article_match.group(1)}条",
            "article_num": chinese_to_arabic(article_match.group(1)),
        }

    # 尝试提取数字
    num_match = re.search(r"(\d+)", citation)
    if num_match:
        return {
            "law_name": citation.split(str(num_match.group(1)))[0]
                        .replace("《", "").replace("》", "").replace("第", "").strip(),
            "article_number": f"第{num_match.group(1)}条",
            "article_num": int(num_match.group(1)),
        }

    return {"law_name": "", "article_number": "", "article_num": 0}


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

    新旧字段并存：status / verified 保持旧调用方可用，
    law_status / article_status / citation_valid / version 是唯一判据。
    """
    pending = status == "pending"

    if message:
        warning = message
    elif pending:
        warning = (f"该条文已公布但尚未生效，{effective_date} 起施行，"
                   "不得作为现行有效依据引用") if effective_date else \
                  "该条文已公布但尚未生效，不得作为现行有效依据引用"
    elif status == "repealed" and law_status != law_state.LAW_EFFECTIVE:
        # 整部法废止才说「该法规已废止」；条文单独废止不能说成整部法废止
        basis = repeal_reason or ""
        if basis.startswith("被") and basis.endswith("废止"):
            basis = basis[1:-2]
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
        "is_pending": pending,
        "warning": warning,
        "effective_date": effective_date,
        "repeal_date": repeal_date,
        "repeal_reason": repeal_reason,
        "replaced_by": replaced_by,
        "is_principle_clause": is_principle_clause,
        "has_multiple_versions": has_multiple_versions,
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
    return {
        "total_articles": total,
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


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)