#!/usr/bin/env python3
# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟

"""G3 生效区间修复：只动 law_versions 的日期字段与派生状态，不重建任何索引。

三类根因与处置：
  1) 补录批次的镜像合并本，施行日被抽出成"原法施行日"（正文附则里的老日期），
     导致新版施行日早于前版。处置：按本地修改决定原文的施行条款回填。
  2) 同一文件重复入库（file_sha256 相同或正文高度一致），两版同日生效。
     处置：清空重复件（superseded）的 effective_date，把它移出生效链。
  3) superseded_at 写成了入库执行时间戳（2026-11-03T02:12:00Z）。
     处置：改成"后继版本施行日"；后继无施行日则置空。G3 判据不读该字段。

用法：
  python3 tools/fix_g3_dates.py                 # 预览 + 出 reports/g3-fix-list.csv
  python3 tools/fix_g3_dates.py --apply         # 写库
"""
from __future__ import annotations

import argparse
import csv
import datetime
import difflib
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent

ORIGINAL_DIRS = [
    "inbox/mirror",
    "inbox/mirror1103",
    "inbox/flk1103",
    os.path.join("下载的法条", "2026年11月3日下载"),
]
DATE_PATTERN = re.compile(r"公布日期(\d{4}-\d{2}-\d{2})_施行日期(\d{4}-\d{2}-\d{2})")
CLAUSE_PUB = re.compile(r"自公布之日起施行")
CLAUSE_DATE = re.compile(r"自(\d{4})年(\d{1,2})月(\d{1,2})日起施行")

SUPERSEDE_REASON = "被新修订版本取代"


def normalize(text: str) -> str:
    return re.sub(r"[《》〈〉\s（）()\u3000]", "", text)


def load_versions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, norm_name, law_name, publish_date, effective_date, status, "
        "superseded_by, superseded_at, file_sha256, source_file FROM law_versions "
        "ORDER BY norm_name, COALESCE(publish_date, ''), id").fetchall()


def find_pairs(versions: list[sqlite3.Row]) -> list[tuple[str, sqlite3.Row, sqlite3.Row]]:
    """与门禁 G3 完全一致的口径。"""
    by_law: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in versions:
        by_law[row["norm_name"]].append(row)
    pairs: list[tuple[str, sqlite3.Row, sqlite3.Row]] = []
    for _, rows in by_law.items():
        dated = [v for v in rows if v["effective_date"]]
        if not dated:
            continue
        prev = None
        for v in dated:
            if prev and v["effective_date"] < prev["effective_date"]:
                pairs.append(("backwards", prev, v))
            prev = v
        seen: dict[str, sqlite3.Row] = {}
        for v in dated:
            other = seen.get(v["effective_date"])
            if other is not None:
                pairs.append(("same_day", other, v))
            else:
                seen[v["effective_date"]] = v
    return pairs


def scan_originals() -> list[dict]:
    out: list[dict] = []
    for rel in ORIGINAL_DIRS:
        d = _ROOT / rel
        if not d.is_dir():
            continue
        for name in os.listdir(d):
            if not ("修改" in name or "决定" in name):
                continue
            m = DATE_PATTERN.search(name)
            if not m:
                continue
            out.append({
                "path": str((d / name).relative_to(_ROOT)),
                "publish": m.group(1),
                "effective": m.group(2),
                "name": name,
            })
    return out


_body_cache: dict[str, str] = {}


def body_of(rel_path: str) -> str:
    if rel_path not in _body_cache:
        p = _ROOT / rel_path
        _body_cache[rel_path] = normalize(
            p.read_text(encoding="utf-8-sig", errors="ignore"))
    return _body_cache[rel_path]


def effective_from_clause(rel_path: str, decision_publish: str) -> tuple[str, str]:
    """从决定正文的施行条款取施行日，返回 (日期, 依据位置)。"""
    p = _ROOT / rel_path
    lines = p.read_text(encoding="utf-8-sig", errors="ignore").splitlines()
    hit = None
    for idx, line in enumerate(lines):
        if "施行" not in line:
            continue
        if CLAUSE_PUB.search(line):
            hit = (decision_publish, f"L{idx + 1} 「本决定自公布之日起施行」")
        else:
            m = CLAUSE_DATE.search(line)
            if m:
                y, mo, da = m.groups()
                hit = (f"{y}-{int(mo):02d}-{int(da):02d}", f"L{idx + 1} {line.strip()[:60]}")
    return hit or (decision_publish, "未找到施行条款，按公布日")


def match_decision(originals: list[dict], law_name: str, publish_date: str) -> dict | None:
    key = normalize(law_name)
    for item in originals:
        if item["publish"] != publish_date:
            continue
        if key in body_of(item["path"]):
            return item
    return None


def text_of_version(conn: sqlite3.Connection, version_id: int) -> str:
    rows = conn.execute(
        "SELECT content FROM law_articles WHERE version_id=? "
        "ORDER BY article_number_num, COALESCE(article_suffix,'')", (version_id,)).fetchall()
    return normalize("".join(r[0] for r in rows))


def is_duplicate(conn: sqlite3.Connection, a: sqlite3.Row, b: sqlite3.Row) -> tuple[bool, str]:
    if a["file_sha256"] and a["file_sha256"] == b["file_sha256"]:
        return True, "file_sha256 完全相同"
    ta, tb = text_of_version(conn, a["id"]), text_of_version(conn, b["id"])
    if not ta or not tb:
        return False, ""
    ratio = difflib.SequenceMatcher(None, ta, tb).quick_ratio()
    if ratio >= 0.99:
        return True, f"正文相似度 {ratio:.3f}"
    return False, ""


def build_plan(conn: sqlite3.Connection, today: str) -> tuple[list[dict], list[dict]]:
    versions = load_versions(conn)
    originals = scan_originals()
    pairs = find_pairs(versions)

    rows: list[dict] = []
    updates: list[dict] = []
    for kind, prev, nxt in pairs:
        row = {
            "kind": "倒挂" if kind == "backwards" else "同日",
            "norm_name": nxt["norm_name"],
            "prev_id": prev["id"],
            "prev_publish": prev["publish_date"] or "",
            "prev_effective": prev["effective_date"] or "",
            "next_id": nxt["id"],
            "next_publish": nxt["publish_date"] or "",
            "next_effective": nxt["effective_date"] or "",
            "next_status": nxt["status"],
            "action": "",
            "new_effective": "",
            "evidence": "",
            "evidence_file": "",
            "evidence_loc": "",
            "note": "",
        }
        if kind == "same_day":
            dup, why = is_duplicate(conn, prev, nxt)
            frozen = conn.execute(
                "SELECT COUNT(*) FROM law_articles WHERE version_id=? AND status!='repealed'",
                (prev["id"],)).fetchone()[0]
            if dup and prev["status"] == "superseded" and frozen == 0:
                row.update(action="重复件移出生效链", new_effective="",
                           evidence="重复入库", evidence_file=prev["source_file"] or "",
                           evidence_loc=f"version_id={prev['id']}",
                           note=f"{why}；该件条文已全部 repealed，保留行以维持可追溯")
                updates.append({"kind": "clear_effective", "id": prev["id"], "value": "",
                                "norm_name": prev["norm_name"], "row": row})
                rows.append(row)
                continue
            points_to_next = prev["superseded_by"] in (None, nxt["id"])
            if prev["status"] == "superseded" and frozen == 0 and points_to_next:
                row.update(action="前版整体废止后移出生效链", new_effective="",
                           evidence="前版条文已全部废止", evidence_file=prev["source_file"] or "",
                           evidence_loc=f"version_id={prev['id']}",
                           note="前版被后版取代且无有效条文，同日生效属残留日期")
                updates.append({"kind": "clear_effective", "id": prev["id"], "value": "",
                                "norm_name": prev["norm_name"], "row": row})
                rows.append(row)
                continue
        decision = match_decision(originals, nxt["law_name"], nxt["publish_date"] or "")
        if decision:
            eff, loc = effective_from_clause(decision["path"], decision["publish"])
            row.update(action="改为决定施行日", new_effective=eff,
                       evidence="修改决定原文", evidence_file=decision["path"], evidence_loc=loc)
            updates.append({"kind": "effective_date", "id": nxt["id"], "value": eff,
                            "norm_name": nxt["norm_name"], "row": row})
        else:
            row.update(action="未修（缺口）", evidence="本地缺修改决定原文",
                       note=f"检索 {', '.join(ORIGINAL_DIRS)} 未找到公布日 {nxt['publish_date']} "
                            f"且正文含该法名的决定；镜像正文只带原法施行日")
        rows.append(row)

    deduped: dict[tuple[str, int], dict] = {}
    for u in updates:
        deduped[(u["kind"], u["id"])] = u
    return rows, list(deduped.values())


def plan_superseded_at(conn: sqlite3.Connection, versions: list[sqlite3.Row]) -> list[dict]:
    """superseded_at 时间戳 → 后继版本施行日（法律失效日）。"""
    by_law: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in versions:
        by_law[row["norm_name"]].append(row)
    out: list[dict] = []
    for _, rows in by_law.items():
        for idx, row in enumerate(rows):
            value = row["superseded_at"] or ""
            if "T" not in value and " " not in value:
                continue
            successor_eff = ""
            for later in rows[idx + 1:]:
                if later["effective_date"]:
                    successor_eff = later["effective_date"]
                    break
            out.append({"id": row["id"], "old": value, "new": successor_eff,
                        "norm_name": row["norm_name"]})
    return out


def plan_restore(conn: sqlite3.Connection, today: str, updates: list[dict]) -> list[dict]:
    """仅对本次改动涉及的法规：有待生效版却无现行版时，恢复前版并解冻条文。"""
    norms = {u["norm_name"] for u in updates}
    out: list[dict] = []
    for norm in sorted(norms):
        future_ids = {u["id"] for u in updates if u["norm_name"] == norm
                      and u["kind"] == "effective_date" and u["value"] > today}
        existing_eff = [r[0] for r in conn.execute(
            "SELECT id FROM law_versions WHERE norm_name=? AND status='effective'", (norm,))]
        if [i for i in existing_eff if i not in future_ids]:
            continue
        pending = conn.execute(
            "SELECT id, publish_date FROM law_versions WHERE norm_name=? AND status='pending' "
            "ORDER BY COALESCE(publish_date,'') DESC, id DESC LIMIT 1", (norm,)).fetchone()
        if pending:
            pending_id, pending_publish = pending[0], pending[1]
        elif future_ids:
            pending_id = next(iter(future_ids))
            pending_publish = conn.execute(
                "SELECT publish_date FROM law_versions WHERE id=?", (pending_id,)).fetchone()[0]
        else:
            continue
        cand = conn.execute(
            "SELECT id, publish_date, effective_date FROM law_versions "
            "WHERE norm_name=? AND id<>? AND status='superseded' "
            "  AND COALESCE(publish_date,'') <= ? "
            "  AND (COALESCE(effective_date,'')='' OR effective_date <= ?) "
            "ORDER BY COALESCE(publish_date,'') DESC, id DESC LIMIT 1",
            (norm, pending_id, pending_publish or "", today)).fetchone()
        if not cand:
            continue
        out.append({"norm_name": norm, "restore_id": cand[0], "pending_id": pending_id,
                    "restore_publish": cand[1] or "", "restore_effective": cand[2] or ""})
    return out


def write_csv(path: Path, rows: list[dict], gaps: list[dict], restore: list[dict],
              superseded: list[dict]) -> None:
    headers = ["序号", "类型", "法名", "前版id", "前版公布日", "前版施行日", "后版id",
               "后版公布日", "后版原施行日", "后版状态", "处置", "修正后施行日",
               "依据类型", "依据文件", "依据位置", "备注"]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(headers)
        for i, row in enumerate(rows, 1):
            w.writerow([i, row["kind"], row["norm_name"], row["prev_id"], row["prev_publish"],
                        row["prev_effective"], row["next_id"], row["next_publish"],
                        row["next_effective"], row["next_status"], row["action"],
                        row["new_effective"], row["evidence"], row["evidence_file"],
                        row["evidence_loc"], row["note"]])
        w.writerow([])
        w.writerow(["附：缺口清单（未修，待补原文）"])
        for i, row in enumerate(gaps, 1):
            w.writerow([f"缺口{i}", row["kind"], row["norm_name"], row["prev_id"],
                        row["prev_publish"], row["prev_effective"], row["next_id"],
                        row["next_publish"], row["next_effective"], row["next_status"],
                        row["action"], "", row["evidence"], "", "", row["note"]])
        w.writerow([])
        w.writerow(["附：现行版本恢复（待生效新版没有现行前版）"])
        for r in restore:
            w.writerow(["恢复", "现行恢复", r["norm_name"], r["restore_id"], r["restore_publish"],
                        r["restore_effective"], r["pending_id"], "", "", "pending",
                        "恢复前版为现行并解冻条文", "", "决定施行日未到", "", "",
                        f"解除 {r['norm_name']} 的现行空档"])
        w.writerow([])
        w.writerow(["附：superseded_at 口径归一（执行时间戳 → 后继施行日）"])
        for r in superseded:
            w.writerow(["时间戳", "口径归一", r["norm_name"], r["id"], "", "", "", "", "", "",
                        "改为后继版本施行日", r["new"], "后继施行日",
                        "law_versions", f"id={r['id']}", f"原值 {r['old']}"])


def apply_plan(conn: sqlite3.Connection, today: str, updates: list[dict], restore: list[dict],
               superseded: list[dict]) -> dict:
    stats = {"effective": 0, "cleared": 0, "pending": 0, "restored": 0,
             "thawed": 0, "superseded_at": 0}
    with conn:
        for u in updates:
            if u["kind"] == "effective_date":
                status = "pending" if u["value"] > today else None
                if status:
                    conn.execute("UPDATE law_versions SET effective_date=?, status=? WHERE id=?",
                                 (u["value"], status, u["id"]))
                    stats["pending"] += 1
                else:
                    conn.execute("UPDATE law_versions SET effective_date=? WHERE id=?", (u["value"], u["id"]))
                stats["effective"] += 1
            else:
                conn.execute("UPDATE law_versions SET effective_date='' WHERE id=?", (u["id"],))
                stats["cleared"] += 1
        for r in restore:
            conn.execute("UPDATE law_versions SET status='effective', superseded_by=NULL, "
                         "superseded_at=NULL WHERE id=?", (r["restore_id"],))
            stats["thawed"] += conn.execute(
                "UPDATE law_articles SET status='effective', repeal_date=NULL, replaced_by=NULL, "
                "repeal_reason=NULL WHERE version_id=? AND status='repealed' "
                "AND (repeal_reason=? OR repeal_reason IS NULL)",
                (r["restore_id"], SUPERSEDE_REASON)).rowcount
            stats["restored"] += 1
        for r in superseded:
            conn.execute("UPDATE law_versions SET superseded_at=? WHERE id=?", (r["new"], r["id"]))
            stats["superseded_at"] += 1
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description="G3 生效区间修复（日期字段）")
    ap.add_argument("--db", default=str(_ROOT / "data" / "law_data.db"))
    ap.add_argument("--today", default=datetime.date.today().isoformat())
    ap.add_argument("--csv", default=str(_ROOT / "reports" / "g3-fix-list.csv"))
    ap.add_argument("--apply", action="store_true", help="真正写库；默认只预览")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    if not args.apply:
        conn.execute("PRAGMA query_only=1")

    versions = load_versions(conn)
    rows, updates = build_plan(conn, args.today)
    gaps = [r for r in rows if r["action"].startswith("未修")]
    restore = plan_restore(conn, args.today, updates)
    superseded = plan_superseded_at(conn, versions)

    write_csv(Path(args.csv), rows, gaps, restore, superseded)

    fixed = len(updates)
    print(f"G3 问题对 {len(rows)}｜可修 {fixed}｜缺口 {len(gaps)}｜"
          f"恢复现行 {len(restore)}｜superseded_at 归一 {len(superseded)}")
    for r in gaps:
        print(f"  [缺口] {r['norm_name']}（id={r['next_id']}，公布 {r['next_publish']}）")
    print(f"清单：{os.path.relpath(args.csv, _ROOT)}")

    if not args.apply:
        print("预览模式，未写库。确认后加 --apply。")
        conn.close()
        return 0

    stats = apply_plan(conn, args.today, updates, restore, superseded)
    conn.close()
    print(json.dumps(stats, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
