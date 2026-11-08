# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""G7 条文重切执行：严格按固定清单重切，只动清单内版本。

硬校验（任一不满足即整体回滚，库保持原样）：
  1. 源文件 sha256 必须与清单一致，且与 `law_versions.file_sha256` 一致；
  2. 清单里的条数与「条号 + 正文」CJK 必须能由当前解析器原样复现；
  3. 逐版本 CJK 差值必须为 0 —— 本轮验收条件；
  4. 写完后逐版本回读复核条数与 CJK。

条文行策略：按 (article_number_num, article_suffix) 原地更新，保住已有 `id`；
新增条号插入新行，消失的条号删除。`vector_id` 按 `source_file|条号` 重算。

不做：不动向量库、不动 fmzy、不推远端。

用法：
  # 预校验（不写库）
  python3 tools/g7_reslice_apply.py --plan reports/g7-reslice-list-20261106.csv
  # 执行
  python3 tools/g7_reslice_apply.py --plan reports/g7-reslice-list-20261106.csv \
      --apply --report reports/g7-reslice-applied-20261107.json
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
BASE = Path(_HERE).parent
sys.path.insert(0, os.path.join(_HERE, "..", "ingest"))
sys.path.insert(0, _HERE)

from parse_markdown import parse_markdown_file  # noqa: E402
from clean_residue import clean_text  # noqa: E402
from schema import (  # noqa: E402
    bump_corpus_version,
    content_hash,
    create_fts,
    meta_get,
    meta_set,
    rebuild_bigram_index,
)

_CJK = re.compile(r"[\u4e00-\u9fff]")
# 章节标题行：旧库常把「第五章 工会的经费和财产」并进上一条正文，
# 重切后标题归 chapter/section 字段。比较 CJK 时两边都剔除标题行，
# 否则标题位移会被误判成正文丢失。
_HEADING_LINE = re.compile(r"^[\s\u3000]*第[一二三四五六七八九十百]+[章节]")


def cjk_count(text: str) -> int:
    return len(_CJK.findall(text or ""))


def _strip_heading_lines(text: str) -> str:
    return "\n".join(
        ln for ln in (text or "").split("\n") if not _HEADING_LINE.match(ln))


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def db_article_state(conn: sqlite3.Connection, version_id: int,
                     strip_headings: bool = True) -> tuple[int, int]:
    """返回 (条数, 条号+正文 CJK)。

    strip_headings=True：剔除正文里的「第X章/第X节」标题行，用于和库内旧状态比对
    （旧库会把章节标题并进上一条正文，重切后标题归 chapter 字段，属位置迁移）。
    strip_headings=False：按存储原文计量，用于写后回读，验证写入内容与解析结果一致。
    """
    rows = conn.execute(
        "SELECT article_number, content FROM law_articles WHERE version_id=? "
        "ORDER BY article_number_num, article_suffix", (version_id,)).fetchall()
    measure = _strip_heading_lines if strip_headings else (lambda t: t or "")
    return len(rows), sum(cjk_count(r[0]) + cjk_count(measure(r[1]))
                          for r in rows)


def preflight(conn: sqlite3.Connection, plan: list[dict],
              allow_cjk_gain: bool = False) -> list[dict]:
    """只读校验清单与源文件；任何一条不符直接抛错。"""
    prepared = []
    for row in plan:
        vid = int(row["version_id"])
        path = BASE / row["source_path"]
        if not path.exists():
            raise SystemExit(f"源文件不存在: {vid} {path}")
        sha = sha256_of(path)
        if sha != row["source_sha256"]:
            raise SystemExit(f"源文件 sha256 与清单不符: {vid} {path.name}")
        v = conn.execute(
            "SELECT law_name, source_file, file_sha256, status FROM law_versions WHERE id=?",
            (vid,)).fetchone()
        if v is None:
            raise SystemExit(f"版本不存在: {vid}")
        if v["source_file"] != path.name:
            raise SystemExit(f"source_file 不匹配: {vid} 库内 {v['source_file']} != {path.name}")
        if (v["file_sha256"] or "") != sha:
            raise SystemExit(f"file_sha256 与源文件不符: {vid} {path.name}")

        expected_count = int(row["new_articles"])
        expected_cjk = int(row["new_cjk"])
        pr = parse_markdown_file(str(path))
        residue_hits: set[str] = set()
        for a in pr.articles:
            cleaned, hits, wounded = clean_text(a.content)
            if wounded:
                raise SystemExit(
                    f"清洗误伤正文: {vid} {a.article_number} 损失 {wounded} 个 CJK")
            if not cleaned.strip():
                raise SystemExit(f"清洗后正文为空: {vid} {a.article_number}")
            a.content = cleaned
            residue_hits.update(hits)
        got_count = len(pr.articles)
        got_cjk = sum(cjk_count(a.article_number) + cjk_count(a.content)
                      for a in pr.articles)
        if got_count != expected_count or got_cjk != expected_cjk:
            raise SystemExit(
                f"解析结果与清单不符: {vid} 条数 {got_count}!={expected_count} "
                f"CJK {got_cjk}!={expected_cjk}")

        keys = [(a.article_number_num, "") for a in pr.articles]
        if len(set(keys)) != len(keys):
            raise SystemExit(f"解析出现重复条号: {vid} {path.name}")

        db_count, db_cjk = db_article_state(conn, vid)
        if db_count != int(row["db_articles"]) or db_cjk != int(row["db_cjk"]):
            raise SystemExit(
                f"库内现状与清单不符: {vid} 条数 {db_count}!={row['db_articles']} "
                f"CJK {db_cjk}!={row['db_cjk']}")
        cjk_delta = expected_cjk - db_cjk
        # 正常重切要求 CJK 守恒；G7 第二轮修复的是“旧解析器整段丢正文”的版本，
        # CJK 只会增加。默认仍要求差值 0，只有显式 --allow-cjk-gain 才放行正差值。
        if cjk_delta != 0 and not (allow_cjk_gain and cjk_delta > 0):
            raise SystemExit(f"逐部 CJK 差值不为 0: {vid} {cjk_delta}")

        prepared.append({"row": row, "vid": vid, "path": path, "sha": sha,
                         "parsed": pr, "status": v["status"], "law_name": v["law_name"],
                         "residue_hits": sorted(residue_hits)})
    return prepared


def reslice_version(conn: sqlite3.Connection, item: dict) -> dict:
    vid = item["vid"]
    pr = item["parsed"]
    row = item["row"]
    v = conn.execute(
        "SELECT law_name, law_type, law_type_label, publish_date, effective_date, "
        "issuing_authority, source_file, law_level, law_province, law_city "
        "FROM law_versions WHERE id=?", (vid,)).fetchone()
    old = conn.execute(
        "SELECT id, article_number_num, article_suffix, status, repeal_date, repeal_reason, "
        "replaced_by, law_level, law_province, law_city FROM law_articles "
        "WHERE version_id=?", (vid,)).fetchall()
    by_key = {(r["article_number_num"], r["article_suffix"] or ""): r for r in old}
    template = old[0] if old else None
    new_keys = {(a.article_number_num, "") for a in pr.articles}

    updated = inserted = deleted = 0
    for a in pr.articles:
        key = (a.article_number_num, "")
        vector_id = f"{v['source_file']}|{a.article_number_num}"
        if key in by_key:
            conn.execute(
                "UPDATE law_articles SET article_number=?, article_number_num=?, "
                "article_suffix=?, chapter=?, section=?, article=?, content=?, "
                "content_hash=?, vector_id=? WHERE id=?",
                (a.article_number, a.article_number_num, "", a.chapter or "", a.section or "",
                 a.content, a.content, content_hash(a.content), vector_id, by_key[key]["id"]))
            updated += 1
        else:
            conn.execute(
                "INSERT INTO law_articles (vector_id, version_id, law_name, law_type, "
                "law_type_label, article_number, article_number_num, article_suffix, chapter, "
                "section, article, content, content_hash, publish_date, effective_date, "
                "issuing_authority, status, repeal_date, repeal_reason, replaced_by, "
                "law_level, law_province, law_city, source_file) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (vector_id, vid, v["law_name"], v["law_type"], v["law_type_label"],
                 a.article_number, a.article_number_num, "", a.chapter or "", a.section or "",
                 a.content, a.content, content_hash(a.content), v["publish_date"],
                 v["effective_date"], v["issuing_authority"],
                 template["status"] if template else "effective",
                 template["repeal_date"] if template else None,
                 template["repeal_reason"] if template else None,
                 template["replaced_by"] if template else None,
                 (template["law_level"] if template else v["law_level"]) or "",
                 (template["law_province"] if template else v["law_province"]) or "",
                 (template["law_city"] if template else v["law_city"]) or "",
                 v["source_file"]))
            inserted += 1
    stale = [r["id"] for k, r in by_key.items() if k not in new_keys]
    if stale:
        marks = ",".join("?" for _ in stale)
        conn.execute(f"DELETE FROM law_articles WHERE id IN ({marks})", stale)
        deleted = len(stale)
    conn.execute("UPDATE law_versions SET article_count=? WHERE id=?",
                 (len(pr.articles), vid))

    # 回读复核（事务内，失败即整体回滚）：按存储原文计量，与解析结果直接比对
    got_count, got_cjk = db_article_state(conn, vid, strip_headings=False)
    exp_count, exp_cjk = int(row["new_articles"]), int(row["new_cjk"])
    if got_count != exp_count or got_cjk != exp_cjk:
        raise SystemExit(
            f"写入后复核失败: {vid} 条数 {got_count}!={exp_count} CJK {got_cjk}!={exp_cjk}")
    return {"version_id": vid, "law_name": item["law_name"], "status": item["status"],
            "articles_before": int(row["db_articles"]), "articles_after": got_count,
            "updated_in_place": updated, "inserted": inserted, "deleted": deleted,
            "cjk": got_cjk, "residue_hits": item["residue_hits"]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(BASE / "data/law_data.db"))
    ap.add_argument("--plan", default=str(BASE / "reports/g7-reslice-list-20261106.csv"))
    ap.add_argument("--apply", action="store_true", help="实际写入；默认只做预校验")
    ap.add_argument("--allow-cjk-gain", action="store_true",
                    help="允许逐部 CJK 相对库为正差值（仅用于修复旧解析器丢正文的版本）")
    ap.add_argument("--report", default=str(BASE / "reports/g7-reslice-applied-20261107.json"))
    args = ap.parse_args()

    plan = list(csv.DictReader(open(args.plan, encoding="utf-8")))
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    t0 = time.perf_counter()

    prepared = preflight(conn, plan, allow_cjk_gain=args.allow_cjk_gain)
    cjk_rule = "允许正差值" if args.allow_cjk_gain else "必须为 0"
    print(f"预校验通过：{len(prepared)} 个版本，CJK 口径={cjk_rule}，"
          f"条数 {sum(int(p['row']['db_articles']) for p in prepared)} -> "
          f"{sum(int(p['row']['new_articles']) for p in prepared)}")
    if not args.apply:
        conn.close()
        return

    before_total = conn.execute("SELECT COUNT(*) FROM law_articles").fetchone()[0]
    conn.execute("BEGIN IMMEDIATE")
    try:
        done = [reslice_version(conn, item) for item in prepared]
        after_total = conn.execute("SELECT COUNT(*) FROM law_articles").fetchone()[0]
        expected_delta = sum(int(p["row"]["new_articles"]) - int(p["row"]["db_articles"])
                             for p in prepared)
        if after_total - before_total != expected_delta:
            raise SystemExit(f"全库条数变化不符: {after_total - before_total} != {expected_delta}")
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    print(f"写入完成：条文 {before_total} -> {after_total}（{after_total - before_total:+d}）")

    t_fts = time.perf_counter()
    fts_ok = create_fts(conn)
    t_bigram = time.perf_counter()
    bigram = rebuild_bigram_index(conn)
    conn.commit()

    corpus_version = bump_corpus_version(conn)
    today = date.today().isoformat()
    cutoff = conn.execute(
        "SELECT MAX(publish_date) FROM law_versions "
        "WHERE publish_date IS NOT NULL AND publish_date != ''").fetchone()[0] or ""
    if cutoff > today:
        cutoff = today
    meta_set(conn, "data_cutoff", cutoff)
    meta_set(conn, "snapshot_date", today)
    conn.commit()

    report = {
        "applied_at": today,
        "plan": os.path.basename(args.plan),
        "versions": len(done),
        "articles_before": before_total,
        "articles_after": after_total,
        "articles_delta": after_total - before_total,
        "updated_in_place": sum(d["updated_in_place"] for d in done),
        "inserted": sum(d["inserted"] for d in done),
        "deleted": sum(d["deleted"] for d in done),
        "fts_rebuilt": fts_ok,
        "bigram": bigram,
        "bigram_meta_before": meta_get(conn, "bigram_index_version"),
        "corpus_version": corpus_version,
        "data_cutoff": cutoff,
        "snapshot_date": today,
        "timing_sec": {"fts": round(t_bigram - t_fts, 1),
                       "bigram": round(time.perf_counter() - t_bigram, 1),
                       "total": round(time.perf_counter() - t0, 1)},
        "details": done,
    }
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "details"},
                     ensure_ascii=False, indent=2))
    conn.close()


if __name__ == "__main__":
    main()
