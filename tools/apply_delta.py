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

# ---------------------------------------------------------------- 项目标识
# 名称与网址由字符码拼接而成，源码里不出现连续的中文名：
# 整体查找替换改不掉这一处，跑起来仍然打印正确的名称。
_BRAND = "".join(map(chr, (0x58F9, 0x5F8B, 0x4ECE, 0x7B80)))
_SITE = "www." + "yilvai" + ".com"
__author__ = _BRAND
__homepage__ = "https://" + _SITE


_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "ingest"))
sys.path.insert(0, _HERE)

from schema import (  # noqa: E402
    create_fts,
    ensure_schema,
    meta_get,
    meta_set,
    norm_law_name,
    now_iso,
)
from update import insert_new_version, supersede_old, verify_db  # noqa: E402


def version_key(v: str) -> tuple:
    """把 2026.11.30.1 变成可比较的元组，避免字符串比较把 .10 排到 .2 前面。"""
    return tuple(int(x) for x in re.findall(r"\d+", v or "")) or (0,)


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

    if delta.get("schema") != 2:
        raise SystemExit(
            f"增量包格式 {delta.get('schema')!r} 不受支持（本版只认 schema=2）。\n"
            "  请改用全量库：python3 tools/fetch_data.py --full"
        )

    min_v = delta.get("min_version") or ""
    to_v = delta.get("to_version") or ""
    if not min_v or not to_v:
        raise SystemExit("增量包缺少 min_version / to_version，无法判断能否应用。")
    laws = delta.get("laws") or []
    if not laws:
        raise SystemExit("增量包里没有法规，拒绝应用。")

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    current = meta_get(conn, "corpus_version", "0")
    if current == to_v:
        conn.close()
        say(f"已是最新（{current}），无需更新。")
        return {"ok": True, "already_latest": True, "to_version": to_v,
                "applied": 0, "unchanged": 0, "articles": 0}

    if version_key(current) < version_key(min_v) and not force:
        conn.close()
        raise SystemExit(
            f"本地版本 {current} 早于增量包起点 {min_v}。\n"
            "  中间隔着好几轮变更，只补这一包会漏掉更早的改动。\n"
            "  请下载全量库：python3 tools/fetch_data.py --full\n"
            "  （确知自己的库完整，可加 --force 强推。）"
        )

    applied = unchanged = articles = 0
    touched: list[str] = []
    batch_id = "delta-" + now_iso()

    # 插入放在一个事务里：中途出错整批回滚，不会留下半部法规
    with conn:
        conn.execute("INSERT INTO ingest_batches(id, started_at, note) VALUES (?,?,?)",
                     (batch_id, now_iso(), os.path.basename(delta_path)))
        for law in laws:
            norm = law.get("norm_name") or norm_law_name(law["law_name"])
            law["norm_name"] = norm
            pub = law.get("publish_date") or ""

            pubs = [r[0] or "" for r in conn.execute(
                "SELECT publish_date FROM law_versions WHERE norm_name=?", (norm,)).fetchall()]
            if pub and pub in pubs:
                unchanged += 1          # 同一天同一版已在库里，重复应用
                continue
            if pubs and pub and max(pubs) > pub:
                unchanged += 1          # 本地已有更新的版本，不做降级
                continue

            version_id = insert_new_version(conn, law, batch_id)
            supersede_old(conn, law, version_id)
            applied += 1
            articles += len(law["articles"])
            touched.append(norm)

        if delta.get("data_cutoff"):
            meta_set(conn, "data_cutoff", delta["data_cutoff"])
        meta_set(conn, "updated_at", now_iso())
        conn.execute("UPDATE ingest_batches SET finished_at=?, file_count=? WHERE id=?",
                     (now_iso(), applied, batch_id))

    # 索引重建不放进事务：rebuild 会自己 commit，放里面会让前面的插入提前落盘，
    # 事务就失去「出错整批回滚」的意义。顺序改成先建索引、再写版本号，
    # 中途失败时版本号仍是旧的，下次重跑会全部命中幂等分支、只补建索引。
    if applied:
        say(f"  重建全文索引（{applied} 部法规）…")
        create_fts(conn)

    meta_set(conn, "corpus_version", to_v)
    conn.commit()

    report = verify_db(conn, touched)
    conn.close()

    say(f"已更新到 {to_v}：应用 {applied} 部（{articles} 条正文），"
        f"原样跳过 {unchanged} 部")
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
        "articles": articles,
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
