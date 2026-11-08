# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""用户侧：把累积增量包应用到本地库（事务 + 幂等 + 版本校验）。

包里的每部法规都是最终态全文，所以「已经是对的」就直接跳过，
重复应用、断点重跑、换机器再跑一遍，都不会插出两份同样的版本。

用法：
  python3 tools/apply_delta.py --delta dist/delta-2026.11.30.1.json.gz
  python3 tools/apply_delta.py --delta delta.json --db ./data/law_data.db
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import uuid


_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "ingest"))
sys.path.insert(0, _HERE)

from schema import (  # noqa: E402
    bigram_ready,
    create_fts,
    ensure_schema,
    index_bigram_articles,
    meta_get,
    meta_set,
    norm_law_name,
    now_iso,
)
from update import verify_db  # noqa: E402
from make_delta import ARTICLE_FP_FIELDS, version_fingerprint  # noqa: E402

# 原位覆盖时要写回的版本列。不含 id / batch_id / created_at：
# 前三个是自增主键与写入账本信息，不该由增量包改写。
# law_level/law_province/law_city 是 schema 4 新增层级列，增量包不携带，
# 覆盖时保留本地原值（旧库就是未回填态，按验收口径不计入差异）。
VERSION_UPDATE_FIELDS = (
    "law_name", "norm_name", "law_type", "law_type_label", "publish_date", "effective_date",
    "issuing_authority", "source_file", "file_sha256", "article_count", "status", "superseded_at",
)
ARTICLE_INSERT_FIELDS = ARTICLE_FP_FIELDS


def version_key(v: str) -> tuple:
    """把 2026.11.30.1 变成可比较的元组，避免字符串比较把 .10 排到 .2 前面。"""
    return tuple(int(x) for x in re.findall(r"\d+", v or "")) or (0,)


def mark_law_repealed(conn: sqlite3.Connection, item: dict) -> tuple[int, int]:
    """按包里的废止标记，把本地这部法的现行版本降级、条文标废止。

    返回（降级的版本数，标废止的条文数）。本地没有这部法时返回 (0, 0)——
    用户不需要在自己库里新增一部已废止的法，跳过即可。

    降级时不写 superseded_by：它是「被某个新版顶掉」才有的指向，整部废止
    没有接班版本，硬塞一个 id 会让核验逻辑指错。判定整部废止靠的是
    「该法名下一条 effective/pending 都不剩」，不是这个字段。
    """
    norm = item.get("norm_name") or norm_law_name(item["law_name"])
    rep_date = item.get("repeal_date") or ""
    rep_reason = item.get("repeal_reason") or ""

    ids = [r[0] for r in conn.execute(
        "SELECT id FROM law_versions WHERE norm_name=? AND status IN ('effective', 'pending')",
        (norm,)).fetchall()]
    if not ids:
        return 0, 0

    n_art = 0
    for vid in ids:
        conn.execute(
            "UPDATE law_versions SET status='superseded', superseded_by=NULL, superseded_at=? "
            "WHERE id=?",
            (now_iso(), vid),
        )
        n_art += conn.execute(
            "UPDATE law_articles SET status='repealed', repeal_date=?, repeal_reason=? "
            "WHERE version_id=? AND (status IS NULL OR status != 'repealed')",
            (rep_date, rep_reason, vid),
        ).rowcount
    return len(ids), n_art


def _local_articles(conn: sqlite3.Connection, version_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM law_articles WHERE version_id=? "
        "ORDER BY article_number_num, COALESCE(article_suffix, '')",
        (version_id,),
    ).fetchall()
    return [{k: r[k] for k in ARTICLE_FP_FIELDS} for r in rows]


def _resolve_superseded_ref(conn: sqlite3.Connection, raw_id, norm: str) -> str:
    """把本机 superseded_by 自增 id 翻成接班版本的公布日，供指纹比对。"""
    if not raw_id:
        return ""
    row = conn.execute(
        "SELECT norm_name, publish_date FROM law_versions WHERE id=?", (raw_id,)).fetchone()
    if row is None or row["norm_name"] != norm:
        return ""
    return row["publish_date"] or ""


def _local_version_fp(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    version = {k: row[k] for k in row.keys()}
    version["superseded_by_ref"] = _resolve_superseded_ref(conn, row["superseded_by"], row["norm_name"])
    return version_fingerprint(version, _local_articles(conn, row["id"]))


def _find_local_version(conn: sqlite3.Connection, entry: dict) -> sqlite3.Row | None:
    """判断这条版本在本地是否已经存在，返回对应行。

    优先用 db_id（同一发布链路上的库 id 一致）；命中不了时退回
    「公布日 + 施行日 + 源文件」的语义匹配，避免跨库 id 不同就整条重复插入。
    """
    norm = entry["norm_name"]
    db_id = entry.get("db_id")
    if db_id:
        row = conn.execute(
            "SELECT * FROM law_versions WHERE id=? AND norm_name=?", (db_id, norm)).fetchone()
        if row is not None:
            return row
    pub = entry.get("publish_date") or ""
    eff = entry.get("effective_date") or ""
    src = entry.get("source_file") or ""
    rows = conn.execute(
        "SELECT * FROM law_versions WHERE norm_name=? AND COALESCE(publish_date,'')=? "
        "AND COALESCE(effective_date,'')=?", (norm, pub, eff)).fetchall()
    if rows:
        for row in rows:
            if (row["source_file"] or "") == src:
                return row
        return rows[0]
    rows = conn.execute(
        "SELECT * FROM law_versions WHERE norm_name=? AND COALESCE(publish_date,'')=?",
        (norm, pub)).fetchall()
    for row in rows:
        if (row["source_file"] or "") == src:
            return row
    if len(rows) == 1:
        return rows[0]
    return None


def _local_superseded_by(conn: sqlite3.Connection, entry: dict) -> int | None:
    """把包里的接班指针落到本地 id；解析不到时保留原值（与正式库的悬挂态一致）。"""
    raw = entry.get("superseded_by")
    if not raw:
        return None
    norm = entry["norm_name"]
    row = conn.execute(
        "SELECT id, norm_name FROM law_versions WHERE id=?", (raw,)).fetchone()
    if row is not None and row["norm_name"] == norm:
        return raw
    ref = entry.get("superseded_by_ref") or ""
    if ref:
        rows = conn.execute(
            "SELECT id FROM law_versions WHERE norm_name=? AND COALESCE(publish_date,'')=? "
            "ORDER BY id", (norm, ref)).fetchall()
        if rows:
            return rows[-1]["id"]
    return raw


def _insert_articles(conn: sqlite3.Connection, version_id: int, entry: dict) -> int:
    rows = []
    for a in entry.get("articles") or []:
        vector_id = f"{a.get('source_file') or ''}|{a.get('article_number_num')}{a.get('article_suffix') or ''}"
        rows.append(
            (vector_id, version_id) + tuple(a.get(k) for k in ARTICLE_INSERT_FIELDS)
        )
    if not rows:
        return 0
    placeholders = ",".join("?" for _ in range(2 + len(ARTICLE_INSERT_FIELDS)))
    conn.executemany(
        f"INSERT INTO law_articles(vector_id, version_id, {', '.join(ARTICLE_INSERT_FIELDS)}) "
        f"VALUES ({placeholders})",
        rows,
    )
    return len(rows)


def _write_version(conn: sqlite3.Connection, entry: dict, batch_id: str) -> tuple[str, int]:
    """把包里的一个版本落到本地：指纹一致跳过，不一致原位覆盖，缺失则插入。

    返回（动作，version_id）。动作取值 unchanged / updated / inserted。
    """
    entry["norm_name"] = entry.get("norm_name") or norm_law_name(entry["law_name"])
    local = _find_local_version(conn, entry)
    superseded_by = _local_superseded_by(conn, entry)

    if local is not None:
        if _local_version_fp(conn, local) == entry.get("version_fp"):
            return "unchanged", local["id"]
        assignments = ", ".join(f"{c}=?" for c in VERSION_UPDATE_FIELDS)
        conn.execute(
            f"UPDATE law_versions SET {assignments}, superseded_by=? WHERE id=?",
            tuple(entry.get(c) for c in VERSION_UPDATE_FIELDS) + (superseded_by, local["id"]),
        )
        conn.execute("DELETE FROM law_articles WHERE version_id=?", (local["id"],))
        _insert_articles(conn, local["id"], entry)
        return "updated", local["id"]

    want_id = entry.get("db_id")
    use_id = None
    if want_id and conn.execute("SELECT 1 FROM law_versions WHERE id=?", (want_id,)).fetchone() is None:
        use_id = want_id
    columns = list(VERSION_UPDATE_FIELDS) + ["superseded_by", "batch_id", "created_at"]
    values = [entry.get(c) for c in VERSION_UPDATE_FIELDS] + [superseded_by, batch_id, now_iso()]
    if use_id is not None:
        columns = ["id"] + columns
        values = [use_id] + values
    cur = conn.execute(
        f"INSERT INTO law_versions({', '.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})",
        values,
    )
    version_id = use_id if use_id is not None else int(cur.lastrowid)
    _insert_articles(conn, version_id, entry)
    return "inserted", version_id


def _delete_version(conn: sqlite3.Connection, item: dict) -> bool:
    """按删除标记清掉本地版本及其条文。返回是否真的删了。"""
    norm = item.get("norm_name")
    vid = None
    db_id = item.get("db_id")
    if db_id:
        row = conn.execute(
            "SELECT id, norm_name FROM law_versions WHERE id=?", (db_id,)).fetchone()
        if row is not None and row["norm_name"] == norm:
            vid = row["id"]
    if vid is None:
        row = conn.execute(
            "SELECT id FROM law_versions WHERE norm_name=? AND COALESCE(publish_date,'')=? "
            "AND COALESCE(source_file,'')=? ORDER BY id LIMIT 1",
            (norm, item.get("publish_date") or "", item.get("source_file") or "")).fetchone()
        if row is not None:
            vid = row["id"]
    if vid is None:
        return False
    conn.execute("DELETE FROM law_articles WHERE version_id=?", (vid,))
    conn.execute("DELETE FROM law_versions WHERE id=?", (vid,))
    return True


def apply_delta_file(delta_path: str, db: str, force: bool = False,
                     verbose: bool = True) -> dict:
    """应用一个增量包。成功返回统计，失败抛 SystemExit（消息可直接给用户看）。"""

    def say(msg: str) -> None:
        if verbose:
            print(msg)

    if not os.path.exists(db):
        raise SystemExit(f"本地库不存在：{db}")
    with open(delta_path, encoding="utf-8") as f:
        delta = json.load(f)

    if delta.get("schema") not in (2, 3):
        raise SystemExit(
            f"增量包格式 {delta.get('schema')!r} 不受支持（本版认 schema=2/3）。\n"
            "  请改用全量库：python3 tools/fetch_data.py --full"
        )

    min_v = delta.get("min_version") or ""
    to_v = delta.get("to_version") or ""
    if not min_v or not to_v:
        raise SystemExit("增量包缺少 min_version / to_version，无法判断能否应用。")
    laws = delta.get("laws") or []
    repealed = delta.get("repealed") or []
    deleted = delta.get("deleted_versions") or []
    if not laws and not repealed and not deleted:
        raise SystemExit("增量包里没有法规，拒绝应用。")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    current = meta_get(conn, "corpus_version", "0")
    if current == to_v:
        conn.close()
        say(f"已是最新（{current}），无需更新。")
        return {"ok": True, "already_latest": True, "to_version": to_v,
                "applied": 0, "unchanged": 0, "articles": 0,
                "repealed": 0, "repealed_articles": 0, "repealed_absent": 0}

    if version_key(current) < version_key(min_v) and not force:
        conn.close()
        raise SystemExit(
            f"本地版本 {current} 早于增量包起点 {min_v}。\n"
            "  中间隔着好几轮变更，只补这一包会漏掉更早的改动。\n"
            "  请下载全量库：python3 tools/fetch_data.py --full\n"
            "  （确知自己的库完整，可加 --force 强推。）"
        )

    applied = unchanged = articles = history_added = 0
    touched: list[str] = []
    version_ids: list[int] = []
    # now_iso() 只到秒。同一秒内重跑（用户连点两次更新、脚本失败重试）会撞
    # ingest_batches 主键直接抛 IntegrityError，幂等承诺就断了。补一段随机后缀：
    # 批次号本来就是给人看日志用的，不承担排序职责。
    batch_id = "delta-" + now_iso() + "-" + uuid.uuid4().hex[:6]

    # 插入放在一个事务里：中途出错整批回滚，不会留下半部法规
    with conn:
        conn.execute("INSERT INTO ingest_batches(id, started_at, note) VALUES (?,?,?)",
                     (batch_id, now_iso(), os.path.basename(delta_path)))
        for law in laws:
            norm = law.get("norm_name") or norm_law_name(law["law_name"])
            law["norm_name"] = norm
            action, version_id = _write_version(conn, law, batch_id)
            if action == "unchanged":
                unchanged += 1
                continue
            version_ids.append(version_id)
            touched.append(norm)
            if law.get("status") == "superseded":
                history_added += 1
            else:
                applied += 1
                articles += len(law["articles"])

        # 删除版本放在版本写入之后：被删版本的 id 还可能被别的版本的
        # superseded_by 指着（正式库里就是悬挂指针），先写指针再删行，
        # 删除后指针保留原值，才和正式库的最终态一致。
        deleted_versions = 0
        for item in deleted:
            if _delete_version(conn, item):
                deleted_versions += 1
                touched.append(item.get("norm_name") or "")

        # 整部废止放在新增/更新之后：同一部法既不可能又出新版又整部废止，
        # 分开跑只是让「降级」这件事在日志里有个独立计数，别混进 unchanged。
        # 本地没有这部法的跳过数单独记：它既不是「已是最新」，也不是「处理了」，
        # 混进 unchanged 会让人以为本地已有 42 部法的最新版。
        repealed_laws = repealed_articles = repealed_absent = 0
        for item in repealed:
            norm = item.get("norm_name") or norm_law_name(item["law_name"])
            n_ver, n_art = mark_law_repealed(conn, item)
            if not n_ver:
                repealed_absent += 1    # 本地没装这部法，或早已标失效
                continue
            repealed_laws += 1
            repealed_articles += n_art
            touched.append(norm)

        if delta.get("data_cutoff"):
            meta_set(conn, "data_cutoff", delta["data_cutoff"])
        meta_set(conn, "updated_at", now_iso())
        conn.execute("UPDATE ingest_batches SET finished_at=?, file_count=? WHERE id=?",
                     (now_iso(), applied + deleted_versions, batch_id))

    # 索引重建不放进事务：rebuild 会自己 commit，放里面会让前面的插入提前落盘，
    # 事务就失去「出错整批回滚」的意义。顺序改成先建索引、再写版本号，
    # 中途失败时版本号仍是旧的，下次重跑会全部命中幂等分支、只补建索引。
    if applied or history_added or deleted_versions:
        say(f"  重建全文索引（{applied} 部现行/待生效 + {history_added} 部历史版）…")
        create_fts(conn)
    # 补录的历史版同样要进二字倒排，否则 as_of 查旧条文时 2 字检索会漏
    if version_ids and bigram_ready(conn):
        placeholders = ",".join("?" for _ in version_ids)
        new_ids = [r[0] for r in conn.execute(
            f"SELECT id FROM law_articles WHERE version_id IN ({placeholders})", version_ids)]
        index_bigram_articles(conn, new_ids)

    meta_set(conn, "corpus_version", to_v)
    conn.commit()

    report = verify_db(conn, touched)
    conn.close()

    say(f"已更新到 {to_v}：应用 {applied} 部现行/待生效（{articles} 条正文），"
        f"原样跳过 {unchanged} 部")
    if history_added:
        say(f"  原位更新/补录历史版本 {history_added} 部")
    if deleted_versions:
        say(f"  删除版本 {deleted_versions} 个")
    if repealed_laws:
        say(f"  另有 {repealed_laws} 部整部废止，{repealed_articles} 条正文已标失效")
    if delta.get("data_cutoff"):
        say(f"  条文截至 {delta['data_cutoff']}")
    say(f"自检：{'通过' if report['ok'] else '发现问题'}")
    for p in report["problems"]:
        say("  - " + p)

    return {
        "ok": report["ok"],
        "already_latest": False,
        "to_version": to_v,
        "applied": applied,
        "unchanged": unchanged,
        "history_added": history_added,
        "deleted_versions": deleted_versions,
        "articles": articles,
        "repealed": repealed_laws,
        "repealed_articles": repealed_articles,
        "repealed_absent": repealed_absent,
        "problems": report["problems"],
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="应用累积增量包")
    ap.add_argument("--delta", required=True, help="增量包路径（.json / .json.gz）")
    ap.add_argument("--db", default="./data/law_data.db")
    ap.add_argument("--force", action="store_true", help="跳过版本下限校验")
    args = ap.parse_args()

    path = args.delta
    tmp = None
    if path.endswith(".gz"):
        import gzip
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".json", prefix="lawdelta-")
        os.close(fd)
        with gzip.open(path, "rb") as src, open(tmp, "wb") as dst:
            dst.write(src.read())
        path = tmp
    try:
        apply_delta_file(path, args.db, args.force)
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


if __name__ == "__main__":
    main()
