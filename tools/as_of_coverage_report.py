#!/usr/bin/env python3
# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""按 as_of 口径列出「库里查不到的历史版本」清单。

判断依据只有库内事实：
  - 每部法的版本区间是连续的（右端点 = 下一版施行日），所以不存在版本之间的空档；
  - 真正覆盖不到的是**最早入库版本施行日之前**，as_of 会返回 no_version_in_force。

因此本清单只给两个确定信息：
  1. 该法在库内能覆盖到的最早日期（coverage_start）；
  2. 该日之前无版本可查，且库内没有更早版本的原文。

更早的版本是否真实存在、叫什么、哪一年公布，本地数据推不出来——
不做推断，是否补录由人工按外部权威原本核对。

输出：reports/as_of-coverage.csv、reports/as_of-gaps.json
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = BASE_DIR / "data" / "law_data.db"
DEFAULT_OUT = BASE_DIR / "reports" / "as_of-coverage.csv"
DEFAULT_STATS = BASE_DIR / "reports" / "as_of-gaps.json"


def main() -> None:
    import sqlite3

    ap = argparse.ArgumentParser(description="as_of 历史版本覆盖清单")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--stats", default=str(DEFAULT_STATS))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    rows = conn.execute(
        "SELECT norm_name, law_name, COALESCE(NULLIF(effective_date,''), publish_date, ''), "
        "       publish_date, status "
        "FROM law_versions WHERE norm_name IS NOT NULL AND norm_name<>'' "
        "ORDER BY norm_name, COALESCE(NULLIF(effective_date,''), publish_date, '')"
    ).fetchall()
    conn.close()

    per_law: dict[str, dict] = {}
    for norm, law_name, start, publish, status in rows:
        item = per_law.setdefault(norm, {
            "norm_name": norm, "law_name": law_name, "versions": 0,
            "coverage_start": "", "earliest_publish": "", "latest_start": "",
            "history_versions": 0,
        })
        item["versions"] += 1
        if status and status != "effective" and status != "pending":
            item["history_versions"] += 1
        if start and (not item["coverage_start"] or start < item["coverage_start"]):
            item["coverage_start"] = start
            item["law_name"] = law_name
        if publish and (not item["earliest_publish"] or publish < item["earliest_publish"]):
            item["earliest_publish"] = publish
        if start > item["latest_start"]:
            item["latest_start"] = start

    out_rows = []
    for item in per_law.values():
        start = item["coverage_start"]
        out_rows.append({
            "norm_name": item["norm_name"],
            "law_name": item["law_name"],
            "versions": item["versions"],
            "history_versions": item["history_versions"],
            "earliest_publish_date": item["earliest_publish"],
            "coverage_start": start,
            "latest_version_start": item["latest_start"],
            "gap_note": f"as_of 早于 {start} 时无版本可查；库内无更早版本原文" if start else "库内版本缺施行日与公布日，无法判定",
        })
    out_rows.sort(key=lambda r: (r["coverage_start"] == "", r["coverage_start"], r["norm_name"]))

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()) if out_rows else ["norm_name"])
        w.writeheader()
        w.writerows(out_rows)

    years = defaultdict(int)
    for r in out_rows:
        years[(r["coverage_start"] or "unknown")[:4]] += 1
    stats = {
        "scope": "按 as_of 口径的库内覆盖清单；只陈述库内事实，不推断更早版本是否存在",
        "laws": len(out_rows),
        "laws_with_multiple_versions": sum(1 for r in out_rows if r["versions"] > 1),
        "laws_with_history_versions": sum(1 for r in out_rows if r["history_versions"] > 0),
        "laws_without_any_date": sum(1 for r in out_rows if not r["coverage_start"]),
        "coverage_start_by_year": dict(sorted(years.items())),
    }
    Path(args.stats).write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in stats.items() if k != "coverage_start_by_year"}, ensure_ascii=False, indent=2))
    print("按 coverage_start 年份分布：", dict(sorted(years.items())[:12]), "…")


if __name__ == "__main__":
    main()
