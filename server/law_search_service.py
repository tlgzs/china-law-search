# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

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

认证：不在本项目范围。本服务默认不校验任何凭据，如需公网暴露，
      请自行前置反向代理或网关做认证与限流。

环境变量（均可选）:
  LAW_DB_PATH          SQLite 路径          默认 <项目>/data/law_data.db
  LAW_CHROMA_PATH      ChromaDB 路径        默认 <项目>/data/legal_vector_db
  LAW_COLLECTION_NAME  Chroma 集合名        默认 chinese_laws
  LAW_EMBEDDING_MODEL  Embedding 模型       默认 BAAI/bge-large-zh-v1.5
  LAW_PORT             监听端口             默认 8001
"""

import datetime
import logging
import math
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

# 离线模式：服务器无外网时，模型已缓存在本地
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("law_search")

# ============================================================
# 法条查询内核（唯一实现，MCP 服务器共用同一份）
# ============================================================
# 条号「之一」、法规名归一这类规则只允许有一份实现：
# 网页与 agent 各写一份，迟早走岔，而 agent 查错法条不会有人复核。
try:  # 以 `uvicorn server.law_search_service:app` 启动
    from server.law_query_core import (  # noqa: PLC0415
        _COLS,
        LawStore,
        _build_verify_result,
        chinese_to_arabic,
        match_terms as _match_terms,
        normalize_law_name,
        parse_article_number,
        parse_article_suffix,
        parse_citation_string as _parse_citation_string,
        resolve_law_names,
        row_to_item as _row_to_item,
        status_rank,
    )
except ImportError:  # 以 `python server/law_search_service.py` 直接启动
    from law_query_core import (  # type: ignore[no-redef]  # noqa: PLC0415
        _COLS,
        LawStore,
        _build_verify_result,
        chinese_to_arabic,
        match_terms as _match_terms,
        normalize_law_name,
        parse_article_number,
        parse_article_suffix,
        parse_citation_string as _parse_citation_string,
        resolve_law_names,
        row_to_item as _row_to_item,
        status_rank,
    )

# 检查更新：判断逻辑与 tools/check_update.py 共用同一份，这里只引入缓存层
try:  # 以 `uvicorn server.law_search_service:app` 启动
    from server import update_checker  # noqa: PLC0415
except ImportError:  # 以 `python server/law_search_service.py` 直接启动
    import update_checker  # type: ignore[no-redef]  # noqa: PLC0415

# ============================================================
# 配置（环境变量 > 默认值；默认相对本项目 data/ 目录）
# ============================================================

_BASE_DIR = Path(__file__).resolve().parent.parent
_DATA_DIR = Path(os.environ.get("LAW_DATA_DIR", str(_BASE_DIR / "data")))


def _read_app_version() -> str:
    """程序版本号的唯一来源：仓库根目录的 VERSION 文件。

    以前这个数字硬编码在 FastAPI(...) 和 /health 两处，改版本要记得改两个地方，
    而「检查程序更新」需要一个能跟 GitHub tag 比对的可信值，散落的硬编码不行。
    读不到就返回 "0.0.0"——宁可显示成未知版本，也不让服务起不来。
    """
    try:
        return (_BASE_DIR / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


APP_VERSION = _read_app_version()

DB_PATH = os.environ.get("LAW_DB_PATH", str(_DATA_DIR / "law_data.db"))
CHROMA_PATH = os.environ.get("LAW_CHROMA_PATH", str(_DATA_DIR / "legal_vector_db"))
COLLECTION_NAME = os.environ.get("LAW_COLLECTION_NAME", "chinese_laws")
EMBEDDING_MODEL = os.environ.get("LAW_EMBEDDING_MODEL", "BAAI/bge-large-zh-v1.5")


# ============================================================
# 条号解析 / 法名归一 —— 实现见 server/law_query_core.py
# ============================================================




# ============================================================
# SQLite 数据库
# ============================================================

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


db = get_db()


def _store() -> LawStore:
    """当前连接上的查询内核（轻对象，按需创建，不缓存连接状态）。"""
    return LawStore(db)


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

app = FastAPI(title="China Law Search API", version=APP_VERSION)


@app.get("/health")
async def health():
    """健康检查 — 返回双库状态。免认证。"""
    result: dict[str, Any] = {
        "status": "ok",
        "version": APP_VERSION,
    }

    # SQLite 状态（count=含历史版本，effective=现行有效，pending=已公布未生效）
    try:
        sqlite_count = db.execute("SELECT COUNT(*) FROM law_articles").fetchone()[0]
        effective_count = db.execute(
            "SELECT COUNT(*) FROM law_articles "
            "WHERE status IS NULL OR status NOT IN ('repealed','pending')"
        ).fetchone()[0]
        pending_count = db.execute(
            "SELECT COUNT(*) FROM law_articles WHERE status = 'pending'"
        ).fetchone()[0]
        result["sqlite"] = {
            "status": "ok",
            "count": sqlite_count,
            "effective": effective_count,
            "pending": pending_count,
        }

        # 数据版本：法条有时效，使用者必须能一眼看到自己查的是哪一版
        try:
            meta = {
                r[0]: r[1]
                for r in db.execute("SELECT key, value FROM corpus_meta").fetchall()
            }
            result["sqlite"]["corpus_version"] = meta.get("corpus_version")
            result["sqlite"]["snapshot_date"] = meta.get("snapshot_date")
            # 条文收录截止日（库内法规公布日期的最大值）。和 corpus_version 是两件事：
            # 版本号说「哪天打的包」，cutoff 说「条文收到哪一天」。律师看的是后者。
            result["sqlite"]["data_cutoff"] = meta.get("data_cutoff") or None
        except sqlite3.Error:
            # 老库没有 corpus_meta 表，不影响健康检查
            result["sqlite"]["corpus_version"] = None
            result["sqlite"]["snapshot_date"] = None
            result["sqlite"]["data_cutoff"] = None
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
    law_level: str | None = Query(default=None, description="层级：national/province/city"),
    province: str | None = Query(default=None, description="省级行政区，如 广东省"),
    city: str | None = Query(default=None, description="市/自治州，如 沈阳市"),
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
        items = _search_via_sqlite(q, k, type, include_history,
                                   law_level=law_level, province=province, city=city)

    # 含历史结果时，现行有效条文一律排在已失效条文之前（stable，不打乱相关性顺序）
    if include_history:
        items.sort(key=lambda x: x.get("is_repealed", False))

    elapsed_ms = int((time.time() - start) * 1000)
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


# _row_to_item / _COLS 已由 server/law_query_core.py 提供



def _search_via_sqlite(q: str, k: int, law_type: str | None,
                       include_history: bool = False, law_level: str | None = None,
                       province: str | None = None, city: str | None = None) -> list[dict]:
    """SQLite 三级检索 — 实现见 server/law_query_core.py:LawStore.search。"""
    return _store().search(q, k=k, law_type=law_type, include_history=include_history,
                           law_level=law_level, province=province, city=city)

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
    as_of: str | None = Query(
        default=None,
        description="按时间点取条文，格式 YYYY-MM-DD。不传则取现行版（与旧行为一致）。"),
):
    """根据法律名称和条号获取法条正文。支持模糊匹配法律名称。"""
    start = time.time()
    if as_of:
        # 版本选择逻辑只在 law_query_core 里实现一份，HTTP 侧直接复用，
        # 避免「网页按现行版、接口按时间点」两套判据各写一遍而走岔。
        item = _store().get_article(law_name, article_num, article_suffix or "",
                                    include_history=include_history, as_of=as_of)
        if not item:
            return {"error": "not_found", "law_name": law_name,
                    "article_num": article_num, "as_of": as_of}
        if item.get("error"):
            return item
        item.setdefault("is_pending", item.get("status") == "pending")
        item.setdefault("is_repealed", item.get("status") == "repealed")
        return item
    # 状态判定只在 law_state 里实现一份。这里以前自己拼 SQL，结果是「被修订取代」
    # 被当成「废止」返回，与 MCP 的结论不一致。现在两条入口共用同一内核。
    item = _store().get_article(law_name, article_num, article_suffix or "",
                                include_history=include_history)
    if not item:
        logger.warning("Not found: law_name=%s, article_num=%d", law_name, article_num)
        return {"error": "not_found", "law_name": law_name, "article_num": article_num}
    if item.get("error"):
        return item
    item.setdefault("is_pending", item.get("status") == "pending")
    item.setdefault("is_repealed", item.get("status") == "repealed")
    logger.info("Found: law_name=%s, article=%s, article_status=%s",
                item.get("law_name"), item.get("article_number"),
                item.get("article_status"))
    return item


@app.get("/api/law/versions")
async def get_law_versions(
    law_name: str = Query(..., min_length=1),
):
    """列出某部法规的全部版本及各自有效区间（施行日 → 下一版施行日前一天）。"""
    versions = _store().list_versions(law_name)
    if not versions:
        return {"error": "not_found", "law_name": law_name,
                "message": "未找到该法规的版本记录，法名可能有出入。"}
    return {
        "law_name": versions[0]["law_name"],
        "version_count": len(versions),
        "versions": versions,
    }



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
):
    """法条批量校验 — 返回完整校验状态（存在性、法律状态、废止信息、原则性条款、多版本歧义）。"""
    # 核验逻辑只在 LawStore.verify 里实现一份。这里以前复制过一遍同样的 SQL，
    # 结果是「并列引文拆分」这类修复补了内核、漏了 HTTP —— agent 调 MCP 能拆，
    # 网页调 HTTP 却只核第一条，还照样报通过。
    data = LawStore(db).verify(payload.citations, include_history=include_history)
    results = data["results"]

    return {
        "results": results,
        "total": len(results),
        "verified_count": sum(1 for r in results if r.get("verified")),
        "failed_count": sum(1 for r in results if not r.get("verified")),
    }








# ============================================================
# API: 库统计
# ============================================================

@app.get("/api/law/stats")
async def law_stats():
    """返回法规库统计信息。"""
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

    effective = db.execute(
        "SELECT COUNT(*) FROM law_articles "
        "WHERE status IS NULL OR status NOT IN ('repealed','pending')"
    ).fetchone()[0]
    pending = db.execute(
        "SELECT COUNT(*) FROM law_articles WHERE status = 'pending'"
    ).fetchone()[0]
    return {
        "total_articles": total,
        "effective_articles": effective,
        # 已公布未生效的条文单列。算进 effective 会让「现行有效」这个数虚高，
        # 而这个数会被写进文档和对外文案。
        "pending_articles": pending,
        "by_type": by_type,
        "chromadb_status": chromadb_status,
        "chromadb_count": chromadb_count,
        "sqlite_status": "ok",
        "embedding_model": EMBEDDING_MODEL if _chroma_available else None,
    }


# ============================================================
# API: 检查数据 / 程序更新
# ============================================================
# 这两个接口只允许本机调用：更新会改用户本地的法条库，网页若被反向代理到公网，
# 不能让任何人都能触发。经代理转发的请求 client.host 是代理地址，正好被挡住。

def _require_local(request: Request) -> None:
    host = request.client.host if request.client else None
    if not update_checker.is_local_host(host):
        raise HTTPException(status_code=403,
                            detail="更新接口只允许在本机调用")


@app.get("/api/law/update-status")
async def update_status(request: Request, force: bool = Query(default=False)):
    """数据与程序版本对比。默认读缓存（TTL 1 小时），force=true 强制联网。"""
    _require_local(request)
    return update_checker.get_status(force=force)


@app.post("/api/law/update-data")
async def update_data(request: Request):
    """下载并安装数据更新（增量优先，失败回落全量）。"""
    _require_local(request)
    result = update_checker.run_update()
    if result.get("ok"):
        return {"ok": True, "to_version": result.get("to_version"),
                "last": update_checker.last_update()}
    return JSONResponse(status_code=409 if result.get("busy") else 500,
                        content=result)


# ============================================================
# 启动事件
# ============================================================

@app.on_event("startup")
async def startup_event():
    """服务启动时初始化 ChromaDB（如果可用），并在后台查一次更新。"""
    logger.info("Law Search Service v%s starting...", APP_VERSION)
    logger.info("SQLite: %s", DB_PATH)
    logger.info("ChromaDB: %s", CHROMA_PATH)

    # 尝试初始化 ChromaDB
    init_chromadb()

    if _chroma_available:
        logger.info("Vector search enabled (ChromaDB)")
    else:
        logger.info("Vector search disabled, using SQLite FTS fallback")

    # 后台查一次有没有新版：不阻塞启动，断网也不影响服务
    if os.environ.get("LAW_UPDATE_CHECK_ON_START", "1") not in ("0", "", "false", "no"):
        update_checker.warm_cache_async(DB_PATH)


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