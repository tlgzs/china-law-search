"""把一份已确认的变更计划打包成增量包（delta），供已装库的用户本地应用。

用法：
  python tools/make_delta.py --plan reports/changes-xxx.plan.json --db data/law_data.db \
      --out dist/delta-2026.10.30.2.json
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

from schema import ensure_schema, meta_get  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="从变更计划生成增量包")
    ap.add_argument("--plan", required=True)
    ap.add_argument("--db", default="./data/law_data.db")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.plan, encoding="utf-8") as f:
        plan = json.load(f)

    conn = sqlite3.connect(args.db)
    ensure_schema(conn)
    to_version = meta_get(conn, "corpus_version", "0")
    conn.close()

    laws = [x for x in plan["laws"] if x["action"] != "skip"]
    delta = {
        "schema": 1,
        "from_version": plan["base_corpus_version"],
        "to_version": to_version,
        "generated_at": plan["generated_at"],
        "laws": laws,
        "summary": {
            "laws": len(laws),
            "added_articles": sum(x["stats"]["added"] for x in laws),
            "changed_articles": sum(x["stats"]["changed"] for x in laws),
        },
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(delta, f, ensure_ascii=False, indent=1)
    print(f"增量包：{args.out}（{len(laws)} 部法规，{os.path.getsize(args.out) / 1024:.0f} KB）")


if __name__ == "__main__":
    main()
