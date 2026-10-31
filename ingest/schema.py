# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""共享 schema 与法条主键规则。

被 ingest/ 与 tools/ 共用：
  - 建表 / 迁移（law_versions、ingest_batches、ingest_files、corpus_meta）
  - 法律名称规范化（同名判定）
  - 法条主键（支持「第一百三十三条之一」这类增补条）
  - 版本号与内容哈希

设计前提：**SQLite 是唯一真源，ChromaDB 只是可重建的索引**。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone

SCHEMA_VERSION = 2

_CN_SUFFIX = re.compile(r"^之([零〇一二三四五六七八九十]+)[\s\u3000]*")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def norm_law_name(name: str) -> str:
    """规范化法律名称，仅用于同名判定，不改写展示用原名。"""
    s = (name or "").strip().replace("《", "").replace("》", "")
    s = re.sub(r"\s+", "", s)
    # 源数据里存在「XX条例_」这种尾带下划线的名称，若不去掉，同一部法会被
    # 拆成两个 norm_name，检索时返回两条结果、其中一条还是错字版。
    s = s.rstrip("_＿")
    if s.startswith("中华人民共和国"):
        s = s[len("中华人民共和国"):]
    return s


def content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def split_suffix(content: str) -> tuple[str, str]:
    """把「之一/之二」从条文开头拆出来。返回 (suffix, content)。"""
    m = _CN_SUFFIX.match(content or "")
    if not m:
        return "", content or ""
    return f"之{m.group(1)}", (content or "")[m.end():].lstrip()


def article_key(num: int, suffix: str = "") -> str:
    return f"{num}{suffix}"


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


CREATE_ARTICLES_SQL = """
CREATE TABLE IF NOT EXISTS law_articles (
    id INTEGER PRIMARY KEY,
    vector_id TEXT,
    version_id INTEGER,
    law_name TEXT NOT NULL,
    law_type TEXT,
    law_type_label TEXT,
    article_number TEXT,
    article_number_num INTEGER,
    article_suffix TEXT DEFAULT '',
    chapter TEXT,
    section TEXT,
    article TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT,
    publish_date TEXT,
    effective_date TEXT,
    issuing_authority TEXT,
    status TEXT DEFAULT 'effective',
    repeal_date TEXT,
    repeal_reason TEXT,
    replaced_by TEXT,
    is_principle_clause INTEGER DEFAULT 0,
    source_file TEXT NOT NULL
);
"""

CREATE_VERSION_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS law_versions (
    id INTEGER PRIMARY KEY,
    law_name TEXT NOT NULL,
    norm_name TEXT NOT NULL,
    law_type TEXT,
    law_type_label TEXT,
    publish_date TEXT,
    effective_date TEXT,
    issuing_authority TEXT,
    source_file TEXT NOT NULL,
    file_sha256 TEXT,
    article_count INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'effective',
    superseded_by INTEGER,
    superseded_at TEXT,
    batch_id TEXT,
    created_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_versions_norm ON law_versions(norm_name, publish_date);
CREATE INDEX IF NOT EXISTS idx_versions_status ON law_versions(status);

CREATE TABLE IF NOT EXISTS ingest_batches (
    id TEXT PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    file_count INTEGER DEFAULT 0,
    added_laws INTEGER DEFAULT 0,
    updated_laws INTEGER DEFAULT 0,
    added_articles INTEGER DEFAULT 0,
    changed_articles INTEGER DEFAULT 0,
    removed_articles INTEGER DEFAULT 0,
    operator TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS ingest_files (
    sha256 TEXT PRIMARY KEY,
    filename TEXT,
    law_name TEXT,
    version_id INTEGER,
    batch_id TEXT,
    processed_at TEXT
);

CREATE TABLE IF NOT EXISTS corpus_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_schema(conn: sqlite3.Connection) -> None:
    """幂等建表 + 老库补列。可在任意版本库上反复执行。"""
    conn.executescript(CREATE_ARTICLES_SQL)
    conn.executescript(CREATE_VERSION_TABLES_SQL)

    existing = _columns(conn, "law_articles")
    additions = {
        "version_id": "INTEGER",
        "article_suffix": "TEXT DEFAULT ''",
        "content_hash": "TEXT",
        "repeal_date": "TEXT",
        "repeal_reason": "TEXT",
        "replaced_by": "TEXT",
        "is_principle_clause": "INTEGER DEFAULT 0",
    }
    for column, ddl in additions.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE law_articles ADD COLUMN {column} {ddl}")

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_articles_version ON law_articles(version_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_articles_key "
        "ON law_articles(version_id, article_number_num, COALESCE(article_suffix,''))"
    )
    conn.execute(
        "INSERT OR IGNORE INTO corpus_meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.execute(
        "INSERT OR IGNORE INTO corpus_meta(key, value) VALUES ('corpus_version', '0')"
    )
    # 条文数据的截止日期（库内法规公布日期的最大值）。与 corpus_version 不同：
    # 版本号说明「这份库是哪天打的包」，data_cutoff 说明「条文收录到哪一天」。
    # 两者混用会让人误以为数据是新的，所以单独记一条。
    conn.execute(
        "INSERT OR IGNORE INTO corpus_meta(key, value) VALUES ('data_cutoff', '')"
    )
    conn.commit()


def create_fts(conn: sqlite3.Connection) -> bool:
    """创建 trigram FTS5 索引；不支持时返回 False（服务端会自动降级 LIKE）。"""
    try:
        conn.executescript(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS law_articles_fts USING fts5(
                law_name, article, content, chapter, section,
                content='law_articles', content_rowid='id', tokenize='trigram'
            );
            """
        )
        conn.execute("INSERT INTO law_articles_fts(law_articles_fts) VALUES('rebuild')")
        conn.commit()
        return True
    except sqlite3.OperationalError:
        return False


def meta_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM corpus_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO corpus_meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def bump_corpus_version(conn: sqlite3.Connection) -> str:
    """数据版本号形如 2026.10.30.3（日期.当日第几次）。"""
    today = datetime.now().strftime("%Y.%m.%d")
    current = meta_get(conn, "corpus_version", "0")
    seq = 1
    if current.startswith(today + "."):
        try:
            seq = int(current.rsplit(".", 1)[1]) + 1
        except ValueError:
            seq = 1
    version = f"{today}.{seq}"
    meta_set(conn, "corpus_version", version)
    meta_set(conn, "updated_at", now_iso())
    conn.commit()
    return version


def latest_version(conn: sqlite3.Connection, norm_name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM law_versions WHERE norm_name = ? "
        "ORDER BY COALESCE(publish_date, '') DESC, id DESC LIMIT 1",
        (norm_name,),
    ).fetchone()
