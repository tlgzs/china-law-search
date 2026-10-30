"""用户侧：把增量包应用到本地库（事务 + 版本校验 + 幂等）。

用法：
  python tools/apply_delta.py --delta dist/delta-2026.10.30.2.json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "ingest"))
sys.path.insert(0, _HERE)

from schema import bump_corpus_version, create_fts, ensure_schema, meta_get, now_iso  # noqa: E402
from update import insert_new_version, supersede_old, verify_db  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="应用增量包")
    ap.add_argument("--delta", required=True)
    ap.add_argument("--db", default="./data/law_data.db")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit(f"本地库不存在：{args.db}")
    with open(args.delta, encoding="utf-8") as f:
        delta = json.load(f)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    current = meta_get(conn, "corpus_version", "0")
    if current == delta["to_version"]:
        print(f"已是最新（{current}），无需更新。")
        return
    if current != delta["from_version"] and not args.force:
        raise SystemExit(
            f"本地版本 {current} 与增量包基准 {delta['from_version']} 不一致。"
            "请下载匹配的增量包或全量库；确需强推请加 --force。"
        )

    batch_id = "delta-" + now_iso()
    conn.execute("INSERT INTO ingest_batches(id, started_at, note) VALUES (?,?,?)",
                 (batch_id, now_iso(), args.delta))
    touched: list[str] = []
    with conn:
        for law in delta["laws"]:
            version_id = insert_new_version(conn, law, batch_id)
            supersede_old(conn, law, version_id)
            touched.append(law["norm_name"])
        create_fts(conn)
        from schema import meta_set
        meta_set(conn, "corpus_version", delta["to_version"])
        meta_set(conn, "updated_at", now_iso())
        conn.execute("UPDATE ingest_batches SET finished_at=?, file_count=? WHERE id=?",
                     (now_iso(), len(delta["laws"]), batch_id))

    report = verify_db(conn, touched)
    conn.close()
    print(f"已更新到 {delta['to_version']}：{len(delta['laws'])} 部法规，"
          f"新增条 {delta['summary']['added_articles']}，修改条 {delta['summary']['changed_articles']}")
    print(f"自检：{'通过' if report['ok'] else '发现问题'}")
    for p in report["problems"]:
        print("  -", p)


if __name__ == "__main__":
    main()
