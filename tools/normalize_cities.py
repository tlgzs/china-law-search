#!/usr/bin/env python3
# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""把库里已有的市名归一到民政部标准名录，并回填省级字段。

只做「名字完全一致」的匹配：
  - 匹配上 → 用标准名录里的省名回填 law_province（已有省名的不覆盖）
  - 匹配不上 → 原样留在库里，并写入 unresolved 清单，不强行模糊匹配

这样做的理由：别名（襄樊→襄阳）、撤市设区（莱芜、巢湖）、写法差异（XX自治州简称）
都需要逐条查证变更公告，模糊匹配会静默改错，而错的地方字段是检索过滤条件，
错了就是「查 A 市法规查出 B 市」。

输入：reports/division-cities.csv（tools/build_division_catalog.py 生成）
输出：reports/city-unresolved.csv、reports/city-normalize-stats.json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(str(Path(__file__).resolve().parent), "..", "ingest"))
from schema import bump_corpus_version  # noqa: E402  语料版本号唯一来源

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = BASE_DIR / "data" / "law_data.db"
DEFAULT_CATALOG = BASE_DIR / "reports" / "division-cities.csv"
DEFAULT_UNRESOLVED = BASE_DIR / "reports" / "city-unresolved.csv"
DEFAULT_STATS = BASE_DIR / "reports" / "city-normalize-stats.json"

MATCHABLE_KINDS = {"设区的市", "不设区的市", "自治州", "地区", "盟", "直辖市"}

_REASON_BY_KIND = {
    "县级市": "县级市，不属于设区的市（无地方立法权口径）",
}


def load_catalog(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with open(path, encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            out[row["name"]] = row
    return out


def classify(names: dict[str, int], catalog: dict[str, dict]) -> tuple[dict, list]:
    matched: dict[str, dict] = {}
    unresolved: list[dict] = []
    for name, count in sorted(names.items()):
        row = catalog.get(name)
        if row and row["kind"] in MATCHABLE_KINDS:
            matched[name] = row
        else:
            reason = (
                _REASON_BY_KIND.get(row["kind"], "")
                if row else "2023 年民政部名录中无同名单位（可能为历史撤并、别名或写法差异）"
            )
            unresolved.append({"name": name, "version_count": count,
                               "reason": reason,
                               "catalog_kind": row["kind"] if row else ""})
    return matched, unresolved


def apply(db: str, matched: dict[str, dict]) -> dict:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    before_empty = conn.execute(
        "SELECT COUNT(*) FROM law_versions WHERE law_level='city' "
        "AND (law_province IS NULL OR law_province='')").fetchone()[0]
    updated_versions = 0
    for name, row in matched.items():
        cur = conn.execute(
            "UPDATE law_versions SET law_province = ? WHERE law_city = ? "
            "AND (law_province IS NULL OR law_province='')",
            (row["province"], name))
        updated_versions += cur.rowcount
    # 条文表按 version_id 同步省字段（只补空值）
    updated_articles = conn.execute(
        "UPDATE law_articles SET law_province = ("
        "  SELECT v.law_province FROM law_versions v WHERE v.id = law_articles.version_id"
        ") WHERE (law_province IS NULL OR law_province='') AND version_id IN ("
        "  SELECT id FROM law_versions WHERE law_province IS NOT NULL AND law_province<>''"
        ")").rowcount

    after_empty = conn.execute(
        "SELECT COUNT(*) FROM law_versions WHERE law_level='city' "
        "AND (law_province IS NULL OR law_province='')").fetchone()[0]
    new_version = bump_corpus_version(conn)
    conn.commit()
    conn.close()
    return {"empty_before": before_empty, "empty_after": after_empty,
            "updated_versions": updated_versions,
            "updated_articles": updated_articles,
            "corpus_version": new_version}


def main() -> None:
    ap = argparse.ArgumentParser(description="市名归一与省字段回填")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--catalog", default=str(DEFAULT_CATALOG))
    ap.add_argument("--unresolved", default=str(DEFAULT_UNRESOLVED))
    ap.add_argument("--stats", default=str(DEFAULT_STATS))
    ap.add_argument("--apply", action="store_true", help="真正写库；不带则只出清单")
    args = ap.parse_args()

    catalog = load_catalog(Path(args.catalog))
    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        "SELECT law_city, COUNT(*) FROM law_versions "
        "WHERE law_city IS NOT NULL AND law_city<>'' GROUP BY law_city").fetchall()
    names = {r[0]: r[1] for r in rows}
    total_versions = sum(names.values())
    conn.close()

    matched, unresolved = classify(names, catalog)

    with open(args.unresolved, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["name", "version_count", "catalog_kind", "reason"])
        w.writeheader()
        w.writerows(unresolved)

    stats = {
        "distinct_city_names": len(names),
        "city_versions": total_versions,
        "matched_names": len(matched),
        "matched_versions": sum(names[n] for n in matched),
        "unresolved_names": len(unresolved),
        "unresolved_versions": sum(x["version_count"] for x in unresolved),
        "applied": bool(args.apply),
    }
    if args.apply:
        stats.update(apply(args.db, matched))
    Path(args.stats).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    print(f"未匹配清单：{args.unresolved}（{len(unresolved)} 个市名）")


if __name__ == "__main__":
    main()
