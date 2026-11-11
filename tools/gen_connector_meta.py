"""按 manifest 生成连接器元信息与上架材料里的数字段。

连接器包和上架材料里的条数、收录类型、语料版本都是对外声明，此前靠手工同步，
入库之后声明与库各说各话。这里把声明收敛成一次生成：数字只来自打包时从库算出的
manifest，本脚本不引入任何新的统计口径，也不改 `version`（连接器版本由人递增值）。

用法：
    python tools/gen_connector_meta.py \\
        --manifest dist/manifest.json \\
        --meta ../docs/workbuddy-connector/yilvai-law/connector-meta.json \\
        --meta "../docs/workbuddy-connector/yilvai-law 2/connector-meta.json" \\
        --listing ../docs/mcp-workbuddy-listing.md \\
        [--db data/law_data.db] [--dry-run]

- `--meta` 可重复，按给定路径逐个更新，只改 description / description_zh /
  description_en / corpus_version 四个字段，其余字段原样保留。
- `--listing` 只替换 markdown 里「数据范围」那一行。
- `--db` 给出时会重算条数与分类型计数并与 manifest 比对，不一致直接退出 1；
  不给则只信 manifest（正式打包后紧接着生成时用这一种）。
- `--dry-run` 只打印将写入的内容，不落盘。
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from typing import NoReturn

# 对外声明收录的类型，顺序即文案顺序。
# 库实测为 0 条时必须人工改这里——不能因为库里暂时没有就悄悄少写一类。
DECLARED_TYPES = ("法律", "行政法规", "地方性法规", "司法解释", "部门规章", "地方政府规章")

# 库里有、但不出现在对外声明里的类型。
# 新增类型时这里和 DECLARED_TYPES 都没覆盖到，脚本会报错要求人工决定。
ACKNOWLEDGED_UNDECLARED = {"宪法", "法律解释", "监察法规", "修改决定"}

EXCLUDED_NOTE = "不包含规范性文件"

_LISTING_ROW = re.compile(r"^\| 数据范围 \|.*\|\s*$", re.MULTILINE)


def _fail(message: str) -> NoReturn:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(1)


def load_manifest(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as f:
        manifest = json.load(f)
    missing = [k for k in ("corpus_version", "article_count_total", "by_law_type") if k not in manifest]
    if missing:
        _fail(
            f"{path} 缺少 {', '.join(missing)}，先用新版 make_release 正式打包生成 manifest，"
            "不要手工补数字"
        )
    return manifest


def verify_against_db(manifest: dict, db: str) -> None:
    """用库重算条数与分类型计数，与 manifest 比对；不一致就拒绝生成声明。"""
    conn = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
    try:
        total = conn.execute("SELECT COUNT(*) FROM law_articles").fetchone()[0]
        by_type = {
            (row[0] or "未分类"): row[1]
            for row in conn.execute(
                "SELECT COALESCE(law_type_label, ''), COUNT(*) FROM law_articles "
                "WHERE status != 'repealed' GROUP BY 1")
        }
    finally:
        conn.close()
    problems = []
    if manifest["article_count_total"] != total:
        problems.append(f"全库法条数不符：manifest {manifest['article_count_total']} / 库 {total}")
    if manifest["by_law_type"] != by_type:
        diff = [
            f"{label}: manifest {manifest['by_law_type'].get(label)} / 库 {by_type.get(label)}"
            for label in sorted(set(manifest["by_law_type"]) | set(by_type))
            if manifest["by_law_type"].get(label) != by_type.get(label)
        ]
        problems.append("分类型条数不符：" + "；".join(diff[:5]))
    if problems:
        _fail("manifest 与库实测不一致，拒绝生成声明：\n  " + "\n  ".join(problems))


def resolve_types(manifest: dict) -> tuple[str, ...]:
    """按 DECLARED_TYPES 核对库实测计数，返回实际对外声明的类型顺序。"""
    by_type = manifest["by_law_type"]
    empty = [t for t in DECLARED_TYPES if not by_type.get(t)]
    if empty:
        _fail(
            f"声明类型在库中为 0 条：{', '.join(empty)}。"
            "库的覆盖范围变了，需人工确认后更新 DECLARED_TYPES 与对外文案"
        )
    unknown = [
        t for t, n in by_type.items()
        if n and t not in DECLARED_TYPES and t not in ACKNOWLEDGED_UNDECLARED
    ]
    if unknown:
        _fail(
            f"库中出现未声明的新类型：{', '.join(sorted(unknown))}。"
            "需人工决定是否写进对外声明，再更新 DECLARED_TYPES 或 ACKNOWLEDGED_UNDECLARED"
        )
    return DECLARED_TYPES


def build_descriptions(manifest: dict, types: tuple[str, ...]) -> tuple[str, str]:
    """返回 (中文, 英文) 两段声明。"""
    total = f"{manifest['article_count_total']:,}"
    version = manifest["corpus_version"]
    type_text = "、".join(types)
    zh = (
        f"检索中国法律法规条文并核验法条引用，法规库共 {total} 条（版本 {version}），"
        f"覆盖{type_text}；{EXCLUDED_NOTE}。"
    )
    en = (
        f"Search {total} Chinese statutes and regulations (law database {version}), "
        "and verify legal citations against the official text."
    )
    return zh, en


def _dump_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def update_meta(path: str, manifest: dict, types: tuple[str, ...], dry_run: bool) -> bool:
    with open(path, encoding="utf-8") as f:
        meta = json.load(f)
    zh, en = build_descriptions(manifest, types)
    before = dict(meta)
    meta["corpus_version"] = manifest["corpus_version"]
    meta["description"] = en
    meta["description_zh"] = zh
    meta["description_en"] = en
    changed = meta != before
    if dry_run:
        print(f"[dry-run] {path}\n{_dump_json(meta)}")
        return changed
    with open(path, "w", encoding="utf-8") as f:
        f.write(_dump_json(meta))
    return changed


def update_listing(path: str, manifest: dict, types: tuple[str, ...], dry_run: bool) -> bool:
    with open(path, encoding="utf-8") as f:
        text = f.read()
    total = f"{manifest['article_count_total']:,}"
    version = manifest["corpus_version"]
    row = (
        f"| 数据范围 | 中国法律法规 {total} 条（法规库版本 {version}；"
        f"{'、'.join(types)}；{EXCLUDED_NOTE}） |"
    )
    if not _LISTING_ROW.search(text):
        _fail(f"{path} 里没有找到「数据范围」表格行，无法生成")
    updated = _LISTING_ROW.sub(row, text, count=1)
    changed = updated != text
    if dry_run:
        print(f"[dry-run] {path}\n{row}")
        return changed
    with open(path, "w", encoding="utf-8") as f:
        f.write(updated)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description="按 manifest 生成连接器与上架材料的声明数字段")
    parser.add_argument("--manifest", default="dist/manifest.json")
    parser.add_argument("--meta", action="append", default=[],
                        help="connector-meta.json 路径，可重复")
    parser.add_argument("--listing", default=None, help="上架材料 markdown 路径")
    parser.add_argument("--db", default=None, help="给出则用库重算并校验 manifest")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    manifest = load_manifest(args.manifest)
    if args.db:
        verify_against_db(manifest, args.db)
    types = resolve_types(manifest)

    for path in args.meta:
        changed = update_meta(path, manifest, types, args.dry_run)
        print(f"{'更新' if changed else '无变化'}：{path}")
    if args.listing:
        changed = update_listing(args.listing, manifest, types, args.dry_run)
        print(f"{'更新' if changed else '无变化'}：{args.listing}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
