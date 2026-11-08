# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""生成 G5 用的应收录清单 reports/catalog.csv。

清单口径（务必看清，别把它当成「全国应当收录多少部法规」的权威数字）：

  - 本清单是**当前全量库的基线快照**：库里有的省级 / 市级地方性法规，逐条列出
    正式名（norm_name）与库内最新公布日。G5 用它做两件事：
      ① 库里少了清单上的某部法（被误删 / 增量包漏写）→ 阻断；
      ② 清单上的公布日比库里新（有新版没进库）→ 告警。
  - 它**不能**证明「本该收录但从未收录」的法一部不缺 —— 那需要一份外部权威清单
    （法名 + 层级 + 最新公布日）。本地只有库本身与已下载目录，推不出这份清单，
    因此写入 `catalog-gaps.csv` 作为缺口，不伪造。

用法：
  python tools/build_catalog.py --db data/law_data.db --out reports/catalog.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3

_LEVEL_LABEL = {"province": "省级", "city": "市级", "national": "国家"}


def build(conn: sqlite3.Connection) -> tuple[list[dict], list[dict], dict]:
    rows = list(conn.execute(
        """SELECT norm_name, law_name, law_level, law_province, law_city,
                  MAX(COALESCE(publish_date, '')) AS latest_publish_date,
                  COUNT(DISTINCT id) AS version_count
           FROM law_versions
           WHERE law_type = 'local_regulation'
           GROUP BY norm_name
           ORDER BY law_level, law_province, law_city, norm_name"""))

    catalog: list[dict] = []
    gaps: list[dict] = []
    for r in rows:
        item = {
            "norm_name": r["norm_name"],
            "latest_publish_date": r["latest_publish_date"] or "",
            "level": _LEVEL_LABEL.get(r["law_level"] or "", "未判定"),
            "province": r["law_province"] or "",
            "city": r["law_city"] or "",
            "version_count": r["version_count"],
            "status": "observed",
            "note": "",
        }
        if r["law_level"] in ("province", "city"):
            catalog.append(item)
            if r["law_level"] == "city" and not r["law_province"]:
                gaps.append({**item, "status": "gap",
                             "note": "市名可推断，所属省无法从名称推导（缺行政区划表）"})
        else:
            gaps.append({**item, "status": "gap",
                         "note": "名称前缀推不出省/市，未纳入清单"})

    stats = {
        "province_laws": sum(1 for c in catalog if c["level"] == "省级"),
        "city_laws": sum(1 for c in catalog if c["level"] == "市级"),
        "province_units": len({c["province"] for c in catalog if c["province"]}),
        "city_units": len({c["city"] for c in catalog if c["city"]}),
        "gap_rows": len(gaps),
        "level_unknown_city_province": sum(
            1 for g in gaps if g["status"] == "gap" and g["level"] == "市级"),
        "scope": "当前库基线快照，非外部权威应收录清单",
    }
    return catalog, gaps, stats


def write_rows(path: str, rows: list[dict], extra_columns: bool = False) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=(
            ["norm_name", "latest_publish_date", "level", "province", "city",
             "version_count", "status", "note"] if extra_columns else
            ["norm_name", "latest_publish_date"]))
        w.writeheader()
        for row in rows:
            w.writerow(row if extra_columns else
                       {"norm_name": row["norm_name"],
                        "latest_publish_date": row["latest_publish_date"]})


def main() -> None:
    ap = argparse.ArgumentParser(description="生成 G5 应收录清单")
    ap.add_argument("--db", default="./data/law_data.db")
    ap.add_argument("--out", default="./reports/catalog.csv")
    ap.add_argument("--gaps", default="./reports/catalog-gaps.csv")
    ap.add_argument("--stats", default="./reports/catalog-stats.json")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    catalog, gaps, stats = build(conn)
    conn.close()

    # 主清单只留 G5 认的两列，附录列放在 gaps 文件里，避免 G5 解析受干扰
    write_rows(args.out, catalog, extra_columns=False)
    write_rows(args.gaps, gaps, extra_columns=True)
    with open(args.stats, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)

    print(f"catalog.csv：省级 {stats['province_laws']} 部 / 市级 {stats['city_laws']} 部"
          f"（省 {stats['province_units']} 个、市 {stats['city_units']} 个）")
    print(f"缺口行 {stats['gap_rows']} 条 → {args.gaps}")
    print(f"统计 → {args.stats}")


if __name__ == "__main__":
    main()
