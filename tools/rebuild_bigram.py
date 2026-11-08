#!/usr/bin/env python3
# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""重建二字倒排索引（发布流程固定步骤）。

任何改动条文正文的操作（域代码清洗、条文切分修复、增量导入）之后都要跑一次：
增量维护只把**新增**条文写进倒排，被改写/删除的条文在索引里的旧 posting 不会
自动消失。查询时 `_lookup_by_ids` 仍会用 LIKE 复核，结果不会错，但无效候选会让
2 字查询退化到接近全表扫。

用法：
  python3 tools/rebuild_bigram.py --db data/law_data.db
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(str(Path(__file__).resolve().parent), "..", "ingest"))
from schema import rebuild_bigram_index  # noqa: E402  索引实现唯一来源


def main() -> None:
    ap = argparse.ArgumentParser(description="重建二字倒排索引")
    ap.add_argument("--db", default=str(Path(__file__).resolve().parent.parent / "data" / "law_data.db"))
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    t0 = time.perf_counter()
    stats = rebuild_bigram_index(conn)
    conn.commit()
    conn.close()
    elapsed = time.perf_counter() - t0
    print(json.dumps({"elapsed_sec": round(elapsed, 1), **stats}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
