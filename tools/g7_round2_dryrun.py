# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""G7 第二轮干跑预演：只读库 + 只读源文件，不写任何数据。

对 backlog 里指定批次的版本，用当前解析器重新解析源文件，统计：
  - 跳号消除数 / 仍缺条号 / 新增条号
  - 与库内现状的 CJK 差值（必须为 0）
  - 重复条号、空正文

用法：
  python3 tools/g7_round2_dryrun.py --batch 1 \
      --report reports/g7-round2-dryrun-batch1.json
  python3 tools/g7_round2_dryrun.py --batch all \
      --report reports/g7-round2-dryrun-all.json
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
BASE = Path(_HERE).parent
sys.path.insert(0, os.path.join(_HERE, "..", "ingest"))

from parse_markdown import parse_markdown_file  # noqa: E402
import parse_markdown as _pm  # noqa: E402

_CJK = re.compile(r"[\u4e00-\u9fff]")


def parse_legacy(path: Path):
    """用修复前的标记规则重解析，作为“旧解析器”基线。"""
    saved_patterns = _pm._ARTICLE_MARKUP_PATTERNS
    saved_prefix = _pm._PRIVATE_USE_PREFIX
    _pm._ARTICLE_MARKUP_PATTERNS = saved_patterns[:2]
    _pm._PRIVATE_USE_PREFIX = re.compile(r"^(?!)")
    try:
        return parse_markdown_file(str(path))
    finally:
        _pm._ARTICLE_MARKUP_PATTERNS = saved_patterns
        _pm._PRIVATE_USE_PREFIX = saved_prefix


def cjk_count(text: str) -> int:
    return len(_CJK.findall(text or ""))


def build_source_index(root: Path) -> dict[str, Path]:
    """文件名 -> 源文件路径。同一文件名只保留第一个命中。"""
    index: dict[str, Path] = {}
    for path in root.glob("*/*.md"):
        index.setdefault(path.name, path)
    return index


def gap_set(numbers: set[int]) -> set[int]:
    """1..max 中缺失的条号。"""
    if not numbers:
        return set()
    return set(range(1, max(numbers) + 1)) - numbers


def evaluate(conn: sqlite3.Connection, row: dict, source: Path) -> dict:
    vid = int(row["version_id"])
    db_rows = conn.execute(
        "SELECT article_number, content FROM law_articles WHERE version_id=? "
        "ORDER BY article_number_num, article_suffix", (vid,)).fetchall()
    db_numbers = {n for (n,) in conn.execute(
        "SELECT article_number_num FROM law_articles WHERE version_id=?", (vid,)
    ).fetchall()}
    db_cjk = sum(cjk_count(r[0]) + cjk_count(r[1]) for r in db_rows)

    parsed = parse_markdown_file(str(source))
    new_numbers = [a.article_number_num for a in parsed.articles]
    new_set = set(new_numbers)
    new_cjk = sum(cjk_count(a.article_number) + cjk_count(a.content) for a in parsed.articles)

    legacy = parse_legacy(source)
    legacy_cjk = sum(cjk_count(a.article_number) + cjk_count(a.content)
                     for a in legacy.articles)

    db_gaps = gap_set(db_numbers)
    new_gaps = gap_set(new_set)
    eliminated = sorted(db_gaps - new_gaps)
    still_missing = sorted(db_gaps & new_gaps)
    added = sorted(new_set - db_numbers)

    return {
        "version_id": vid,
        "law_name": row["law_name"],
        "root_cause": row["root_cause"],
        "source_file": row["source_file"],
        "db_articles": len(db_rows),
        "new_articles": len(parsed.articles),
        "legacy_articles": len(legacy.articles),
        "db_max_num": max(db_numbers) if db_numbers else 0,
        "new_max_num": max(new_set) if new_set else 0,
        "db_gaps": len(db_gaps),
        "new_gaps": len(new_gaps),
        "eliminated": len(eliminated),
        "still_missing": still_missing,
        "added_numbers": added,
        "cjk_db": db_cjk,
        "cjk_legacy": legacy_cjk,
        "cjk_new": new_cjk,
        "cjk_diff_db": new_cjk - db_cjk,
        "cjk_diff_legacy": new_cjk - legacy_cjk,
        "legacy_vs_db": legacy_cjk - db_cjk,
        "duplicate_numbers": sorted(
            {n for n in new_numbers if new_numbers.count(n) > 1}),
        "empty_content": sum(1 for a in parsed.articles if not a.content.strip()),
    }


def summarize(items: list[dict]) -> dict:
    fixed = [i for i in items if i["db_gaps"] > 0 and i["new_gaps"] == 0]
    partial = [i for i in items if i["db_gaps"] > 0 and 0 < i["new_gaps"] < i["db_gaps"]]
    unchanged = [i for i in items if i["db_gaps"] > 0 and i["new_gaps"] == i["db_gaps"]]
    regressed = [i for i in items if i["new_gaps"] > i["db_gaps"]]
    cjk_loss = [i for i in items if i["cjk_diff_legacy"] < 0]
    legacy_mismatch = [i for i in items if i["legacy_vs_db"] != 0]
    dup_bad = [i for i in items if i["duplicate_numbers"]]
    empty_bad = [i for i in items if i["empty_content"] > 0]
    return {
        "versions": len(items),
        "gaps_eliminated_total": sum(i["eliminated"] for i in items),
        "fully_fixed": len(fixed),
        "partially_fixed": len(partial),
        "unchanged": len(unchanged),
        "regressed": len(regressed),
        "still_blocking": sum(1 for i in items if i["new_gaps"] > 0),
        "cjk_recovered_total": sum(i["cjk_diff_legacy"] for i in items),
        "cjk_loss_versions": len(cjk_loss),
        "legacy_db_mismatch_versions": len(legacy_mismatch),
        "duplicate_versions": len(dup_bad),
        "empty_content_versions": len(empty_bad),
        "articles_before": sum(i["db_articles"] for i in items),
        "articles_after": sum(i["new_articles"] for i in items),
        "articles_delta": sum(i["new_articles"] - i["db_articles"] for i in items),
        "lists": {
            "fully_fixed": sorted(i["version_id"] for i in fixed),
            "still_missing": sorted(i["version_id"] for i in items if i["new_gaps"] > 0),
            "regressed": sorted(i["version_id"] for i in regressed),
            "cjk_loss": sorted(i["version_id"] for i in cjk_loss),
            "legacy_db_mismatch": sorted(i["version_id"] for i in legacy_mismatch),
            "duplicate": sorted(i["version_id"] for i in dup_bad),
            "empty_content": sorted(i["version_id"] for i in empty_bad),
        },
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(BASE / "data/law_data.db"))
    ap.add_argument("--backlog", default=str(BASE / "reports/g7-round2-backlog-20261107.csv"))
    ap.add_argument("--source-root", default=str(BASE / "inbox/flk-full"))
    ap.add_argument("--batch", default="all", help="1/2/3/4/all")
    ap.add_argument("--report", default=str(BASE / "reports/g7-round2-dryrun.json"))
    args = ap.parse_args()

    rows = list(csv.DictReader(open(args.backlog, encoding="utf-8")))
    if args.batch != "all":
        rows = [r for r in rows if r["batch"] == args.batch]

    index = build_source_index(Path(args.source_root))
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)

    items: list[dict] = []
    no_source: list[dict] = []
    for row in rows:
        source = index.get(row["source_file"])
        if source is None:
            no_source.append({"version_id": int(row["version_id"]),
                              "law_name": row["law_name"],
                              "source_file": row["source_file"]})
            continue
        items.append(evaluate(conn, row, source))
    conn.close()

    report = {
        "batch": args.batch,
        "source_root": args.source_root,
        "summary": summarize(items),
        "no_source": no_source,
        "details": items,
    }
    Path(args.report).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    s = report["summary"]
    print(json.dumps({k: v for k, v in s.items() if k != "lists"},
                     ensure_ascii=False, indent=2))
    print(f"报告: {args.report}")


if __name__ == "__main__":
    main()
