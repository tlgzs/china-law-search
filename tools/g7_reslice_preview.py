"""G7 重切只读预演（保守口径）：库内信号定位范围 + 源文件守恒校验。

只读：`mode=ro` 打开 law_data.db，不改库、不建索引、不写任何表。

步骤：
  1. 用库内条文正文定位受影响版本（含 U+2028 / **第X条** / [第X条]{...} /
     句号后同行条号）。这是「实际坏在哪里」的唯一可信证据。
  2. 为每个版本找源文件：先按 law_versions.source_file 精确匹配本地 inbox；
     找不到再按「规范法名 + 公布日期」匹配 法律法规库/*_markdown。
  3. 用新解析规则 dry-run 切分，按「条号 + 正文」合计 CJK 与库内对照：
     - 合计 CJK 不减少 → 源文件可信，计入条数变化统计；
     - 合计 CJK 减少 → 源文件与库内不是同一份（或真丢字），单独列出，
       不计入「改善」统计，实际重切前必须人工确认。
  4. 输出逐版本条数变化、条文行数、CJK 对照、条号序列 diff。

用法：
  python3 tools/g7_reslice_preview.py --db data/law_data.db \
      --out reports/g7-reslice-preview-20261105.md
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import os
import re
import sqlite3
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "ingest"))

from parse_markdown import parse_markdown_file  # noqa: E402
from schema import norm_law_name  # noqa: E402

BASE = Path(_HERE).parent
WORKSPACE = Path("/Users/fumin/Documents/G个人学习/AI学习/lawyer_model（远程开发）")
LAW_BASE = WORKSPACE / "法律法规库"
# flk-full 为 2026-11-06 从 fmzy 只读拉回的全量原始语料（24,648 个 md），
# 文件名与 law_versions.source_file 同形，排在首位以便精确匹配优先命中。
INBOX_DIRS = [
    BASE / "inbox/flk-full",
    BASE / "inbox/mirror",
    BASE / "inbox/mirror1103",
    BASE / "inbox/flk1103",
]
MD_DIRS = [
    "法律_markdown", "行政法规_markdown", "地方法规_markdown",
    "司法解释_markdown", "监察法规_markdown", "宪法_markdown",
]

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
INLINE_RE = re.compile(r"。第[零〇一二三四五六七八九十百千万0-9]+条[\s\u3000]")
MARKUP_RE = re.compile(
    r"(?:\*\*|__)第[零〇一二三四五六七八九十百千万0-9]+条(?:\*\*|__)|\[第[^\]]+条\]\{"
)
ARTICLE_LINE_RE = re.compile(r"^第[零〇一二三四五六七八九十百千万0-9]+条\s*$")
FILE_DATE_RE = re.compile(r"(?:公布日期)?(20\d{2})-?(\d{2})-?(\d{2})")

SIGNAL_SQL = """
SELECT DISTINCT version_id FROM law_articles
WHERE content LIKE '%'||char(8232)||'%'
   OR content LIKE '%'||char(8233)||'%'
   OR content LIKE '%**第%条**%'
   OR content LIKE '%[第%条]{%'
"""


def cjk_count(text: str) -> int:
    return len(CJK_RE.findall(text or ""))


def file_date(name: str) -> str:
    m = FILE_DATE_RE.search(name)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


def source_key(name: str) -> str:
    s = re.sub(r"^\d{5}_[0-9a-f]+_", "", name)
    s = re.sub(r"^[+*·]", "", s)
    s = re.sub(r"\.md$", "", s)
    s = re.split(r"_(?:公布日期|施行日期|法律法规分类)", s)[0]
    s = re.sub(r"_\d{8}$", "", s)
    return norm_law_name(s)


def file_signals(text: str) -> list[str]:
    lines = text.replace("\r\n", "\n").split("\n")
    hits = []
    if "\u2028" in text or "\u2029" in text:
        hits.append("U2028")
    if INLINE_RE.search(text):
        hits.append("句号后")
    if MARKUP_RE.search(text):
        hits.append("标记条号")
    first = next((i for i, ln in enumerate(lines)
                  if ARTICLE_LINE_RE.match(ln.strip())), None)
    if first is not None and any(ln.strip().startswith(">") and cjk_count(ln)
                                 for ln in lines[first + 1:]):
        hits.append("正文引用块")
    if any(ARTICLE_LINE_RE.match(ln.strip()) for ln in lines):
        hits.append("条号独立行")
    return hits


def missing_of(nums: list[int]) -> str:
    if not nums:
        return "[]"
    s = set(nums)
    holes = [n for n in range(1, max(nums) + 1) if n not in s]
    if not holes:
        return "无"
    return f"{len(holes)}个:[{', '.join(map(str, holes[:12]))}{'…' if len(holes) > 12 else ''}]"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(BASE / "data/law_data.db"))
    ap.add_argument("--out", default=str(BASE / "reports/g7-reslice-preview-20261105.md"))
    ap.add_argument("--csv", default=str(BASE / "reports/g7-reslice-preview-20261105.csv"))
    ap.add_argument("--plan", default=str(BASE / "reports/g7-reslice-list-20261106.csv"))
    args = ap.parse_args()

    # 本地精确源文件索引（flk-full 按子目录存放，故递归匹配）
    exact: dict[str, tuple[Path, str]] = {}
    for d in INBOX_DIRS:
        if d.exists():
            for f in d.rglob("*.md"):
                exact.setdefault(f.name, (f, str(f.relative_to(BASE))))
    # 法律法规库按法名 + 日期索引
    named: dict[str, list[tuple[str, Path]]] = collections.defaultdict(list)
    for d in MD_DIRS:
        p = LAW_BASE / d
        if p.exists():
            for f in p.glob("*.md"):
                named[source_key(f.name)].append((file_date(f.name), f))

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    total_versions = conn.execute("SELECT COUNT(*) FROM law_versions").fetchone()[0]

    scope_ids = [r[0] for r in conn.execute(SIGNAL_SQL)]
    # 句号后同行条号用 SQL 粗筛后再用正则确认
    period_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT version_id FROM law_articles WHERE content LIKE '%'||char(12290)||'第%条%'")]
    scope_ids = sorted(set(scope_ids) | set(period_ids))

    rows = []
    unusable = []
    no_src = []
    for vid in scope_ids:
        v = conn.execute(
            "SELECT id, law_name, law_type, status, publish_date, effective_date, source_file "
            "FROM law_versions WHERE id=?", (vid,)).fetchone()
        if v is None:
            continue
        arts = conn.execute(
            "SELECT article_number, article_number_num, content FROM law_articles "
            "WHERE version_id=? ORDER BY article_number_num, article_suffix", (vid,)).fetchall()
        db_text = "\n".join((a["article_number"] or "") + "\n" + (a["content"] or "") for a in arts)
        db_cjk = cjk_count(db_text)
        db_nums = sorted({int(a["article_number_num"] or 0) for a in arts} - {0})

        hit = exact.get(v["source_file"])
        path = hit[0] if hit else None
        if path is None:
            cands = named.get(norm_law_name(v["law_name"]), [])
            path = next((p for d, p in cands if d == (v["publish_date"] or "")), None)
        if path is None:
            no_src.append((vid, v["law_name"], v["publish_date"] or ""))
            continue

        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="gbk")
        sig = file_signals(text)
        try:
            pr = parse_markdown_file(str(path))
        except Exception as exc:  # noqa: BLE001
            unusable.append((vid, v["law_name"], "解析失败", str(exc)))
            continue

        new_text = "\n".join((a.article_number or "") + "\n" + a.content for a in pr.articles)
        new_cjk = cjk_count(new_text)
        new_nums = sorted({a.article_number_num for a in pr.articles if a.article_number_num})
        base = {
            "vid": vid, "law": v["law_name"], "status": v["status"],
            "db_count": len(arts), "new_count": pr.article_count, "delta": pr.article_count - len(arts),
            "db_cjk": db_cjk, "new_cjk": new_cjk, "cjk_delta": new_cjk - db_cjk,
            "db_missing": missing_of(db_nums), "new_missing": missing_of(new_nums),
            "sig": "/".join(sig) or "-", "src": path.name, "src_kind": "exact" if hit else "named",
            "publish": v["publish_date"] or "", "effective": v["effective_date"] or "",
            "src_path": hit[1] if hit else str(path.relative_to(BASE)),
            "src_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            # A 级：源文件命中规则且条号+正文 CJK 完全一致 —— 条数变化可归因于
            #        新规则，且文本一字未增未减，这才是可信的预演证据。
            # B 级：CJK 有增，源文件与库内文本不一致（可能捞回旧解析丢掉的正文，
            #        也可能混入别部法），必须先确认来源。
            # C 级：CJK 减少，不得重切。
            # D 级：源文件没有任何规则信号，但库内有信号 —— 无法归因，不采信。
            "tier": ("A" if (sig and new_cjk == db_cjk)
                     else "B" if new_cjk > db_cjk
                     else "C" if new_cjk < db_cjk
                     else "D"),
            "has_signal": bool(sig),
        }
        # C 级（CJK 减少）不计入改善
        if new_cjk < db_cjk:
            base["reason"] = "源文件与库内不是同一份或解析丢字"
            unusable.append(base)
        else:
            rows.append(base)
    conn.close()

    rows.sort(key=lambda x: (-abs(x["delta"]), -abs(x["cjk_delta"])))
    tier_a = [r for r in rows if r["tier"] == "A"]
    tier_b = [r for r in rows if r["tier"] == "B"]
    tier_d = [r for r in rows if r["tier"] == "D"]
    a_db = sum(r["db_count"] for r in tier_a)
    a_new = sum(r["new_count"] for r in tier_a)
    a_laws = len({r["law"] for r in tier_a})
    a_up = sum(1 for r in tier_a if r["delta"] > 0)
    a_down = sum(1 for r in tier_a if r["delta"] < 0)
    a_same = sum(1 for r in tier_a if r["delta"] == 0)
    b_laws = len({r["law"] for r in tier_b})

    lines = [
        "# G7 重切只读预演报告（2026-11-06）",
        "",
        "> 只读预演：用新增 5 条解析规则对源文件 dry-run 切分。未改 `data/law_data.db`、",
        "> 未重建 FTS / 二字倒排 / 向量库，未进入实际重切。",
        "",
        "## 1. 结论先行",
        "",
        "**A 级（可信）：源文件与库内文本逐字一致（条号 + 正文 CJK 差值为 0），条数变化纯粹来自切分。**",
        "",
        f"- 涉及版本：**{len(tier_a)}**；涉及法：**{a_laws}** 部",
        f"- 条文行数：库内 {a_db} → 新切 **{a_new}**（净增 {a_new - a_db}）",
        f"- 条数变化：变多 {a_up} 部、变少 {a_down} 部、不变 {a_same} 部",
        f"- CJK 正文：逐部差值 **全部为 0**（守恒）",
        "",
        "**B 级（存疑）：源文件文本与库内不一致，不计入改善，重切前必须确认来源。**",
        "",
        f"- 涉及版本：{len(tier_b)} / {b_laws} 部法；CJK 差值合计 {sum(r['cjk_delta'] for r in tier_b):+d}",
        "",
        f"**C 级（不可用）：CJK 减少，已单列。** 共 {len(unusable)} 个版本。",
        f"**D 级（无法归因）：源文件没有规则信号、库内却有信号。** 共 {len(tier_d)} 个版本，不采信。",
        f"**无源文件：** {len(no_src)} 个版本无法预演。",
        "",
        "## 2. 语料来源",
        "",
        f"- 库内共 {total_versions} 个版本，`law_versions.source_file` 为 `00001_<hash>_名称.md` 形态。",
        "- 2026-11-06 从 fmzy `/home/fumin/law_ingest/法律法规库/` **只读**拉回全量语料至 `inbox/flk-full/`",
        "  （24,648 个 md / 479MB）；其中 23,559 个与 `source_file` 精确同名，",
        "  sha256 与库内 `file_sha256` 全部一致（0 不符）。",
        "- 匹配顺序：精确同名优先；无精确同名时才按「规范法名 + 公布日期」回退匹配",
        "  `法律法规库/*_markdown`（该目录非原始语料，仅作兜底，结果会标注 src_kind=named）。",
        "- 因此用「条号 + 正文 CJK 逐字一致」作为源文件同一性判据：一致的才进 A 级，",
        "  不一致的进 B 级（可能是新规则捞回旧解析丢掉的正文，也可能是混入他法，需人工确认）。",
        "",
        "## 3. A 级：可信条数变化（CJK 逐部守恒，差值为 0）",
        "",
        "按状态分布：",
        "",
        "| 状态 | 版本数 | 库内条数 | 新切条数 | 净增 |",
        "|---|---|---|---|---|",
    ]
    for st in sorted({r["status"] for r in tier_a}):
        grp = [r for r in tier_a if r["status"] == st]
        lines.append(
            f"| {st} | {len(grp)} | {sum(r['db_count'] for r in grp)} "
            f"| {sum(r['new_count'] for r in grp)} | {sum(r['new_count'] - r['db_count'] for r in grp):+d} |"
        )
    lines += [
        "",
        "| 版本 | 法名 | 状态 | 库内条数 | 新切条数 | Δ | 命中规则 | 源文件 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(tier_a, key=lambda x: -x["delta"])[:100]:
        lines.append(
            f"| {r['vid']} | {r['law']} | {r['status']} | {r['db_count']} | {r['new_count']} "
            f"| {r['delta']:+d} | {r['sig']} | {r['src']} |"
        )
    if not tier_a:
        lines.append("| （无） | | | | | | | |")

    lines += [
        "",
        "## 4. B 级：源文件文本不一致（未计入改善，需确认来源）",
        "",
        "| 版本 | 法名 | 库内条数 | 新切条数 | Δ | 库内CJK | 新切CJK | ΔCJK | 命中规则 | 源文件 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(tier_b, key=lambda x: -x["cjk_delta"])[:60]:
        lines.append(
            f"| {r['vid']} | {r['law']} | {r['db_count']} | {r['new_count']} | {r['delta']:+d} "
            f"| {r['db_cjk']} | {r['new_cjk']} | {r['cjk_delta']:+d} | {r['sig']} | {r['src']} |"
        )
    if not tier_b:
        lines.append("| （无） | | | | | | | | | |")

    lines += [
        "",
        "## 4b. D 级：源文件无规则信号、库内却有信号（无法归因，不采信）",
        "",
        "| 版本 | 法名 | 库内条数 | 新切条数 | Δ | 源文件 |",
        "|---|---|---|---|---|---|",
    ]
    for r in sorted(tier_d, key=lambda x: -abs(x["delta"]))[:40]:
        lines.append(
            f"| {r['vid']} | {r['law']} | {r['db_count']} | {r['new_count']} | {r['delta']:+d} | {r['src']} |"
        )
    if not tier_d:
        lines.append("| （无） | | | | | |")

    lines += [
        "",
        "## 5. C 级：CJK 减少（源文件对不上或解析丢字，不得重切）",
        "",
        "| 版本 | 法名 | 库内条数 | 新切条数 | Δ | 库内CJK | 新切CJK | ΔCJK | 源文件 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(unusable, key=lambda x: x.get("cjk_delta", 0))[:40]:
        if isinstance(r, tuple):
            continue
        lines.append(
            f"| {r['vid']} | {r['law']} | {r['db_count']} | {r['new_count']} | {r['delta']:+d} "
            f"| {r['db_cjk']} | {r['new_cjk']} | {r['cjk_delta']:+d} | {r['src']} |"
        )
    if not unusable:
        lines.append("| （无） | | | | | | | | |")

    lines += [
        "",
        "## 6. 找不到源文件的版本（无法预演）",
        "",
        f"共 {len(no_src)} 个。全部列出：",
        "",
        "| 版本 | 法名 | 公布日期 |",
        "|---|---|---|",
    ]
    for vid, law, pub in no_src:
        lines.append(f"| {vid} | {law} | {pub} |")
    if not no_src:
        lines.append("| （无） | | |")

    lines += [
        "",
        "## 7. A 级条号序列 diff（缺号有变化的版本）",
        "",
        "| 版本 | 法名 | 库内缺号 | 新切缺号 |",
        "|---|---|---|---|",
    ]
    seq = [r for r in tier_a if r["db_missing"] != r["new_missing"]]
    for r in seq[:60]:
        lines.append(f"| {r['vid']} | {r['law']} | {r['db_missing']} | {r['new_missing']} |")
    if not seq:
        lines.append("| （无） | | | |")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")

    csv_path = Path(args.csv)
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        f.write("tier,version_id,law_name,status,db_articles,new_articles,delta,"
                "db_cjk,new_cjk,cjk_delta,db_missing,new_missing,signals,source_file\n")
        for r in sorted(rows, key=lambda x: (x["tier"], x["vid"])):
            f.write(f"{r['tier']},{r['vid']},\"{r['law']}\",{r['status']},"
                    f"{r['db_count']},{r['new_count']},{r['delta']},"
                    f"{r['db_cjk']},{r['new_cjk']},{r['cjk_delta']},"
                    f"\"{r['db_missing']}\",\"{r['new_missing']}\",{r['sig']},\"{r['src']}\"\n")
        for r in unusable:
            if isinstance(r, tuple):
                continue
            f.write(f"C,{r['vid']},\"{r['law']}\",{r['status']},{r['db_count']},{r['new_count']},"
                    f"{r['delta']},{r['db_cjk']},{r['new_cjk']},{r['cjk_delta']},"
                    f"\"{r['db_missing']}\",\"{r['new_missing']}\",{r['sig']},\"{r['src']}\"\n")
    print(f"已写入 {csv_path}")

    print(f"已写入 {out}")
    print(f"信号版本 {len(scope_ids)} / A级 {len(tier_a)} / B级 {len(tier_b)} / C级 {len(unusable)} / 无源 {len(no_src)}")
    print(f"A级条数 {a_db} -> {a_new}，法 {a_laws} 部")

    # 固定重切清单：只列 A 级（源文件与库内 CJK 逐字守恒），供用户过目后再执行
    plan = Path(args.plan)
    plan.parent.mkdir(parents=True, exist_ok=True)
    with open(plan, "w", encoding="utf-8", newline="") as f:
        f.write("version_id,law_name,status,publish_date,effective_date,source_path,"
                "source_sha256,db_articles,new_articles,delta,db_cjk,new_cjk,cjk_delta,signals\n")
        for r in sorted(tier_a, key=lambda x: (x["law"], x["vid"])):
            f.write(f"{r['vid']},\"{r['law']}\",{r['status']},{r['publish']},{r['effective']},"
                    f"\"{r['src_path']}\",{r['src_sha256']},{r['db_count']},{r['new_count']},"
                    f"{r['delta']},{r['db_cjk']},{r['new_cjk']},{r['cjk_delta']},\"{r['sig']}\"\n")
    print(f"已写入 {plan}（A 级 {len(tier_a)} 行）")


if __name__ == "__main__":
    main()
