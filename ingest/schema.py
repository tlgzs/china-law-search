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
import time
from array import array
from datetime import datetime, timezone
from typing import Iterator

# 5：law_versions 新增 order_no/source_url/source_gap（部门规章、地方政府规章入库）。
# 均为可空列，老客户端按列名查询，不读这三列也能正常工作，属于向后兼容的加列。
SCHEMA_VERSION = 5

# 法规/条文时效状态：
#   effective  现行有效
#   pending    已公布、尚未生效（施行日在今天之后）
#   superseded 已被更新版本取代（版本级，旧版全文仍保留供旧法适用）
#   repealed   已废止（条文级）
# pending 是第三版新增。缺了它，一部明年才施行的法会被标成「现行有效」，
# 律师据此写进意见书就是引用了还没生效的条文 —— 这是硬伤，不是显示问题。
STATUS_EFFECTIVE = "effective"
STATUS_PENDING = "pending"
STATUS_SUPERSEDED = "superseded"
STATUS_REPEALED = "repealed"

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
    law_level TEXT,
    law_province TEXT,
    law_city TEXT,
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
    created_at TEXT,
    law_level TEXT,
    law_province TEXT,
    law_city TEXT,
    order_no TEXT,
    source_url TEXT,
    source_gap TEXT
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

-- 二字（bigram）倒排索引：FTS5 trigram 索引只切 3 字窗口，2 字查询无法命中，
-- 只能退回 LIKE 全表扫描（实测 2.8–10.8 秒）。这里把每部条文出现过的
-- 二字片段预先建成 posting 列表，2 字查询降为毫秒级，且结果集与 LIKE 完全一致。
-- postings 是升序条文的增量 varint 编码，一个词一行；比 (gram, aid) 明细表小一个数量级。
CREATE TABLE IF NOT EXISTS law_bigram_index (
    gram TEXT PRIMARY KEY,
    n INTEGER NOT NULL,
    postings BLOB NOT NULL
) WITHOUT ROWID;

-- 增量包新入库的条文只写这张小表，2 字查询时与上面的主索引归并；
-- 下次全量重建时并入主索引并清空。避免每次增量都重扫 100 万条正文。
CREATE TABLE IF NOT EXISTS law_bigram_add (
    gram TEXT NOT NULL,
    article_id INTEGER NOT NULL,
    PRIMARY KEY (gram, article_id)
) WITHOUT ROWID;
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def initial_status(effective_date: str | None, today: str | None = None) -> str:
    """入库时该给什么状态。

    施行日在今天之后 → pending（已公布未生效）。没有施行日期时按 effective：
    源数据里缺施行日期的，绝大多数是早已施行的老法规，按 pending 存反而会漏检索。
    """
    eff = (effective_date or "").strip()
    if not eff:
        return STATUS_EFFECTIVE
    today = today or datetime.now().strftime("%Y-%m-%d")
    return STATUS_PENDING if eff > today else STATUS_EFFECTIVE


def _supersede_stale_versions(conn: sqlite3.Connection, today: str) -> int:
    """同一部法同时存在多个现行版本时，只留最新的一个，其余降级并作废条文。

    这是「新版到施行日才算取代旧版」的落点：调进来之前 pending 已经翻成
    effective，所以这里降级的都是「确实已经被新版顶掉」的旧版，不会误伤
    还没生效的新版（它那一刻还是 pending，不在此列）。
    返回改动的行数（版本 + 条文）。
    """
    norms = conn.execute(
        "SELECT norm_name FROM law_versions WHERE status=? "
        "GROUP BY norm_name HAVING COUNT(*) > 1",
        (STATUS_EFFECTIVE,),
    ).fetchall()
    changed = 0
    for row in norms:
        norm = row[0]
        rows = conn.execute(
            "SELECT id, law_name, effective_date FROM law_versions "
            "WHERE norm_name=? AND status=? "
            "ORDER BY COALESCE(publish_date,'') DESC, id DESC",
            (norm, STATUS_EFFECTIVE),
        ).fetchall()
        keep_id, keep_name, keep_eff = rows[0][0], rows[0][1], (rows[0][2] or "")
        for old in rows[1:]:
            changed += conn.execute(
                "UPDATE law_versions SET status=?, superseded_by=?, superseded_at=? WHERE id=?",
                (STATUS_SUPERSEDED, keep_id, now_iso(), old[0]),
            ).rowcount
            changed += conn.execute(
                "UPDATE law_articles SET status=?, repeal_date=?, replaced_by=?, repeal_reason=? "
                "WHERE version_id=? AND (status IS NULL OR status != ?)",
                (STATUS_REPEALED, keep_eff or today, keep_name, "被新修订版本取代",
                 old[0], STATUS_REPEALED),
            ).rowcount
    return changed


def refresh_pending_status(conn: sqlite3.Connection, today: str | None = None) -> int:
    """按当天日期校正 pending / effective，返回改动条数。

    为什么必须有这一步：status 是入库那一刻的快照，而日期一直在往前走。
    《医疗保障法》入库时标 pending，到了 2027-01-01 它就该是现行有效了。
    只靠入库判定，库会越用越旧、越旧越错。

    反方向（effective 但施行日在未来）也要处理：老库第一次升级到 schema 3 时，
    里面已经有一批「明年才施行却标着现行有效」的版本。

    ★ 单向的日期比对不够：新版翻成 effective 的那一刻，同名的旧版必须同时
    降级，否则这部法会有两个现行版本；反过来，新版还 pending 的时候绝不能
    动旧版 —— 那段时间旧版才是律师该引的文本。
    """
    today = today or datetime.now().strftime("%Y-%m-%d")
    changed = 0
    changed += conn.execute(
        "UPDATE law_versions SET status=? WHERE status=? AND effective_date != '' AND effective_date <= ?",
        (STATUS_EFFECTIVE, STATUS_PENDING, today),
    ).rowcount
    changed += conn.execute(
        "UPDATE law_versions SET status=? WHERE status=? AND effective_date != '' AND effective_date > ?",
        (STATUS_PENDING, STATUS_EFFECTIVE, today),
    ).rowcount
    changed += conn.execute(
        "UPDATE law_articles SET status=? WHERE status=? AND effective_date != '' AND effective_date <= ?",
        (STATUS_EFFECTIVE, STATUS_PENDING, today),
    ).rowcount
    changed += conn.execute(
        "UPDATE law_articles SET status=? WHERE status=? AND effective_date != '' AND effective_date > ?",
        (STATUS_PENDING, STATUS_EFFECTIVE, today),
    ).rowcount
    # 上面四步做完，pending 已到期的版本已是 effective，此时才允许降级旧版
    changed += _supersede_stale_versions(conn, today)
    return changed


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
        "law_level": "TEXT",
        "law_province": "TEXT",
        "law_city": "TEXT",
    }
    for column, ddl in additions.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE law_articles ADD COLUMN {column} {ddl}")

    # law_versions 也要补同样的三列：层级是「版本」的属性，条文列是为了检索过滤
    # 不必再 JOIN 版本表。两处同名列由入库路径同时写入，口径一致。
    existing_versions = _columns(conn, "law_versions")
    for column in ("law_level", "law_province", "law_city"):
        if column not in existing_versions:
            conn.execute(f"ALTER TABLE law_versions ADD COLUMN {column} TEXT")
    # 规章入库新增：令号、原文 URL、源侧缺条标记。老库就地补列，不改已有数据。
    for column in ("order_no", "source_url", "source_gap"):
        if column not in existing_versions:
            conn.execute(f"ALTER TABLE law_versions ADD COLUMN {column} TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_versions_level ON law_versions(law_level)"
    )

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_articles_version ON law_articles(version_id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_articles_key "
        "ON law_articles(version_id, article_number_num, COALESCE(article_suffix,''))"
    )
    # 引文核验走的是 law_name + 条号两条等值条件（server/law_query_core.py 的
    # get_article），而上面两条索引都以 version_id 打头，用不上 law_name。
    # 缺这条索引时每核一条要扫 100 万行、约 0.5 秒；补上后是毫秒级。
    # 老库首次启动会就地建一次（约 10 秒），之后 IF NOT EXISTS 是纯元数据查询。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_articles_law_num "
        "ON law_articles(law_name, article_number_num)"
    )
    # 向量增量重建按 source_file 逐版本定位条文（ingest/rebuild_vectors_by_version.py
    # 与 fmzy 的 rebuild_vectors_incremental.py）。缺这条索引时每个版本要扫 100 万行、
    # 约 37 秒，9298 个版本相当于数十小时；补上后按文件定位是毫秒级。
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_articles_source_file "
        "ON law_articles(source_file)"
    )
    # 必须用 upsert：INSERT OR IGNORE 在老库上不会更新已存在的键，
    # 结果就是代码升到 schema 3 了、库里还写着 2，靠 schema_version 做的判断全部失效。
    conn.execute(
        "INSERT INTO corpus_meta(key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
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
    # 状态随日期漂移，每次打开库就地校正一次。只扫 status='pending' 的小集合、
    # 走 idx_versions_status，代价可以忽略；换来的是「查到的时效状态永远对得上今天」。
    refresh_pending_status(conn)
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


# ============================================================
# 二字倒排索引（FTS5 trigram 的 2 字补丁）
# ============================================================

BIGRAM_META_KEY = "bigram_index_version"
BIGRAM_INDEX_VERSION = 1
_BIGRAM_BATCH = 2000


def _encode_ids(ids) -> bytes:
    """升序条文 id → 增量 varint。相邻 id 差值通常是个位数，平均不到 1 字节/条。"""
    out = bytearray()
    prev = 0
    for i in ids:
        d = i - prev
        prev = i
        while d > 127:
            out.append((d & 0x7F) | 0x80)
            d >>= 7
        out.append(d)
    return bytes(out)


def _decode_ids(blob: bytes) -> Iterator[int]:
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


def _merge_sorted(left: Iterator[int], right: Iterator[int]) -> Iterator[int]:
    a = next(left, None)
    b = next(right, None)
    while a is not None or b is not None:
        if b is None or (a is not None and a <= b):
            yield a
            a = next(left, None)
        else:
            yield b
            b = next(right, None)


def _text_grams(text: str, article: str | None = None) -> set[str]:
    """把一段正文的全部二字片段收进集合。

    不过滤标点与换行：查询可能是任意 2 个字，索引漏了就会和 LIKE 结果对不上。
    """
    grams: set[str] = set()
    for i in range(len(text) - 1):
        grams.add(text[i:i + 2])
    if article and article != text:
        for i in range(len(article) - 1):
            grams.add(article[i:i + 2])
    return grams


def rebuild_bigram_index(conn: sqlite3.Connection) -> dict:
    """全量重建二字索引（约 1~3 分钟，+110 MB）。

    只在两种时机调用：全量建库之后、发版打包之前。增量包走 law_bigram_add，
    不重扫全库 —— 否则用户每次更新都要等三分钟。
    """
    started = time.time()
    conn.execute("DELETE FROM law_bigram_index")
    conn.execute("DELETE FROM law_bigram_add")

    buckets: dict[str, array] = {}
    rows = 0
    for aid, content, article in conn.execute(
            "SELECT id, content, article FROM law_articles ORDER BY id"):
        rows += 1
        for g in _text_grams(content or "", article):
            bucket = buckets.get(g)
            if bucket is None:
                buckets[g] = array("I", [aid])
            else:
                bucket.append(aid)

    items = [(g, len(ids), _encode_ids(ids)) for g, ids in buckets.items()]
    for i in range(0, len(items), _BIGRAM_BATCH):
        conn.executemany(
            "INSERT OR REPLACE INTO law_bigram_index(gram, n, postings) VALUES (?,?,?)",
            items[i:i + _BIGRAM_BATCH],
        )

    meta_set(conn, BIGRAM_META_KEY, str(BIGRAM_INDEX_VERSION))
    conn.commit()
    total_postings = sum(b[1] for b in items)
    return {
        "rows": rows,
        "grams": len(items),
        "postings": total_postings,
        "seconds": round(time.time() - started, 1),
    }


def bigram_ready(conn: sqlite3.Connection) -> bool:
    """索引存在且版本对得上才敢用，否则退回 LIKE（宁可慢，不能漏）。"""
    try:
        return meta_get(conn, BIGRAM_META_KEY, "") == str(BIGRAM_INDEX_VERSION)
    except sqlite3.OperationalError:
        return False


def index_bigram_articles(conn: sqlite3.Connection, article_ids) -> int:
    """增量入库后只登记新增条文的二字片段，供 2 字查询与主索引归并。"""
    ids = list(dict.fromkeys(int(x) for x in article_ids))
    if not ids:
        return 0
    written = 0
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        records: list[tuple[str, int]] = []
        for aid, content, article in conn.execute(
                f"SELECT id, content, article FROM law_articles WHERE id IN ({marks})", chunk):
            for g in _text_grams(content or "", article):
                records.append((g, aid))
        if records:
            conn.executemany(
                "INSERT OR IGNORE INTO law_bigram_add(gram, article_id) VALUES (?,?)", records)
            written += len(records)
    return written


def iter_bigram_article_ids(conn: sqlite3.Connection, gram: str) -> Iterator[int]:
    """按条文 id 升序产出含该二字片段的条文，主索引与增量登记表归并。"""
    row = conn.execute(
        "SELECT postings FROM law_bigram_index WHERE gram = ?", (gram,)).fetchone()
    base = _decode_ids(row[0]) if row and row[0] else iter(())
    added = [r[0] for r in conn.execute(
        "SELECT article_id FROM law_bigram_add WHERE gram = ? ORDER BY article_id", (gram,))]
    return _merge_sorted(base, iter(added))
