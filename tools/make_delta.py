# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟

"""把「变更账本」里所有法规的当前有效全文打成累积增量包，供已装库的用户本地应用。

和「一版配一版」的增量不同，这里不要求用户的本地版本正好等于上一次发版：
包里放的是账本内每部法规的**最终态全文**，用户的库无论停在哪一版，
只要不早于 min_version，把这些法规整部替换掉就是最新的。

  用户 A 装了 10 月的库 → 下 11 月的包 → 3 MB
  用户 B 装了 8 月的库  → 下 11 月的包 → 还是 3 MB（不是全量 658 MB）

同一部法在账本里改过多次，包里只存最终态一份，所以体积随「改过多少部法」增长，
不随发版次数叠加。

用法：
  # 并入本次变更并出包（发版前的标准动作）
  python tools/make_delta.py --plan reports/changes-xxx.plan.json --out dist/delta-2026.11.30.1.json

  # 只按现有账本重新出包（账本没变、只是重打一次）
  python tools/make_delta.py --out dist/delta-2026.11.30.1.json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date


_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "ingest"))
sys.path.insert(0, _HERE)

from schema import ensure_schema, meta_get  # noqa: E402

DEFAULT_LEDGER = os.path.normpath(os.path.join(_HERE, "..", "state", "change-ledger.json"))


def load_ledger(path: str) -> dict:
    if not os.path.exists(path):
        return {
            "schema": 1,
            "min_version": "",
            "since": date.today().isoformat(),
            "updated_at": "",
            "laws": {},
        }
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def dump_law(conn: sqlite3.Connection, norm_name: str) -> dict | None:
    """把一部法的当前有效版本连同全部条文还原成「计划里那种 law 对象」。

    结构与 diff_articles.build_plan 产出的条目保持一致，好让 apply 端直接复用
    update.insert_new_version，不必为增量包再写一套插入逻辑。
    """
    v = conn.execute(
        "SELECT * FROM law_versions WHERE norm_name=? AND status='effective' "
        "ORDER BY COALESCE(publish_date,'') DESC, id DESC LIMIT 1",
        (norm_name,),
    ).fetchone()
    if v is None:
        return None

    rows = conn.execute(
        "SELECT * FROM law_articles WHERE version_id=? "
        "ORDER BY article_number_num, COALESCE(article_suffix,'')",
        (v["id"],),
    ).fetchall()

    articles = []
    for a in rows:
        articles.append({
            "law_name": a["law_name"] or v["law_name"],
            "law_type": a["law_type"] or v["law_type"],
            "law_type_label": a["law_type_label"] or v["law_type_label"],
            "article_number": a["article_number"],
            "article_number_num": a["article_number_num"],
            "article_suffix": a["article_suffix"] or "",
            "chapter": a["chapter"],
            "section": a["section"],
            "content": a["content"],
            "content_hash": a["content_hash"],
            "publish_date": a["publish_date"] or v["publish_date"],
            "effective_date": a["effective_date"] or v["effective_date"],
            "issuing_authority": a["issuing_authority"] or v["issuing_authority"],
            "source_file": a["source_file"] or v["source_file"],
        })

    return {
        "law_name": v["law_name"],
        "norm_name": v["norm_name"],
        "law_type": v["law_type"],
        "law_type_label": v["law_type_label"],
        "publish_date": v["publish_date"],
        "effective_date": v["effective_date"],
        "issuing_authority": v["issuing_authority"],
        "source_file": v["source_file"] or f"{v['norm_name']}.md",
        "sha256": v["file_sha256"] or "",
        "articles": articles,
    }


def merge_plan(ledger: dict, plan_path: str) -> tuple[int, int, int]:
    """把本次变更并入账本。返回（新增条目，刷新日期，跳过）。

    只收 new / update。stale 是「镜像版本比库里旧」的，收进账本等于把用户
    本地的新版降级回旧版——这正是原来那份增量包会干的事（实测混进 97 部）。
    """
    with open(plan_path, encoding="utf-8") as f:
        plan = json.load(f)

    added = refreshed = skipped = 0
    for law in plan["laws"]:
        action = law.get("action")
        if action not in ("new", "update"):
            skipped += 1
            continue
        norm = law.get("norm_name")
        if not norm:
            continue
        stamp = law.get("publish_date") or date.today().isoformat()
        old = ledger["laws"].get(norm)
        if old is None:
            ledger["laws"][norm] = stamp
            added += 1
        elif stamp > old:
            ledger["laws"][norm] = stamp
            refreshed += 1
    return added, refreshed, skipped


def main() -> None:
    ap = argparse.ArgumentParser(description="生成累积增量包")
    ap.add_argument("--plan", default=None, help="本次变更计划（可选，给了就并入账本）")
    ap.add_argument("--db", default="./data/law_data.db")
    ap.add_argument("--ledger", default=DEFAULT_LEDGER)
    ap.add_argument("--out", required=True)
    ap.add_argument("--no-ledger-write", action="store_true", help="只读账本，不写回")
    args = ap.parse_args()

    ledger = load_ledger(args.ledger)

    if args.plan:
        added, refreshed, skipped = merge_plan(ledger, args.plan)
        print(f"账本并入：新增 {added} 部，刷新 {refreshed} 部，忽略 {skipped} 条（skip/stale）")

    if not ledger["laws"]:
        raise SystemExit("账本里没有法规，增量包会是空的。先带 --plan 跑一次把账本建起来。")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    to_version = meta_get(conn, "corpus_version", "0")
    data_cutoff = meta_get(conn, "data_cutoff", "")
    if not ledger.get("min_version"):
        # 首次建账本：起点就是当前这版，所有装了这版的人以后都能走增量
        ledger["min_version"] = to_version
    if not ledger.get("since"):
        ledger["since"] = date.today().isoformat()

    laws = []
    missing = []
    empty = []
    for norm in sorted(ledger["laws"]):
        law = dump_law(conn, norm)
        if law is None:
            missing.append(norm)
            continue
        if not law["articles"]:
            empty.append(norm)
            continue
        laws.append(law)
    conn.close()

    if missing:
        print(f"警告：{len(missing)} 部在库里没有生效版本，未进包：{', '.join(missing[:8])}"
              + (" …" if len(missing) > 8 else ""))
    if empty:
        print(f"警告：{len(empty)} 部切不出条文，未进包：{', '.join(empty[:8])}"
              + (" …" if len(empty) > 8 else ""))

    if not laws:
        raise SystemExit("一部法规都没取到，拒绝生成空包。")

    total_articles = sum(len(x["articles"]) for x in laws)
    delta = {
        "schema": 2,
        "min_version": ledger["min_version"],
        "to_version": to_version,
        "data_cutoff": data_cutoff,
        "generated_at": date.today().isoformat(),
        "ledger_since": ledger.get("since", ""),
        "laws": laws,
        "summary": {
            "laws": len(laws),
            "articles": total_articles,
        },
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(delta, f, ensure_ascii=False, indent=1)

    size_mb = os.path.getsize(args.out) / 1048576
    print(f"累积增量包：{args.out}")
    print(f"  {len(laws)} 部法规 / {total_articles} 条正文 ｜ {size_mb:.1f} MB 未压缩")
    print(f"  适用：本地版本 ≥ {ledger['min_version']}（更早的会回落全量）")
    print(f"  目标：{to_version}，条文截至 {data_cutoff or '未知'}")

    if not args.no_ledger_write:
        ledger["updated_at"] = date.today().isoformat()
        ledger["laws"] = {k: ledger["laws"][k] for k in sorted(ledger["laws"])}
        os.makedirs(os.path.dirname(os.path.abspath(args.ledger)), exist_ok=True)
        with open(args.ledger, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False, indent=1)
        print(f"  账本已更新：{args.ledger}（{len(ledger['laws'])} 部）")


if __name__ == "__main__":
    main()
