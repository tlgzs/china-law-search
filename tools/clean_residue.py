# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""清洗 Word 域代码残渣：PAGE / SHAPE / EMBED / ROMAN 域与 HYPERLINK 域、_Toc 锚点。

背景：源数据里有 2,199 部法是从 Word 转出来的，页脚域代码（`PAGE  \\* MERGEFORMAT - 12 -`）
被当成正文尾部拼进了最后一条条文。后果有两个：
  1. 检索误命中 —— 查「格式」会把一堆法条捞出来，正文里却没有这个词；
  2. 引用带脏字 —— 写进意见书的条文末尾挂着 `PAGE \\* MERGEFORMAT`。

用法：
  # 干跑（默认）：只统计、取样、写报告，不改库
  python tools/clean_residue.py --db data/law_data.db --report reports/residue-dryrun.json

  # 实洗（先备份库！）：
  python tools/clean_residue.py --db data/law_data.db --apply --report reports/residue-applied.json

安全阀门：
  - 只删域代码、页码装饰与锚点标记，不删任何中文字符；工具会统计「删除的中文字符数」，
    不为 0 即视为误伤，立即整体回滚（整个事务 rollback，库不变）。
  - 未命中残渣的条文一行不动，`content` 与 `article` 逐列独立清洗。
  - 清洗后重建 FTS 索引（content 变了，索引必须跟着变），并 bump 数据版本号。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "ingest"))

from schema import (  # noqa: E402
    bump_corpus_version,
    content_hash,
    create_fts,
    ensure_schema,
    meta_get,
)

# 域代码：owner + 控制码 + MERGEFORMAT。owner 必须整体匹配，
# 不能只删 `\* MERGEFORMAT`，否则会留下孤零零的 `PAGE`、`SHAPE` 或 `= 1 \* ROMAN`。
_FIELD_CODES = [
    ("page_field_code", re.compile(r"PAGE[ \t]*\\\*[ \t]*MERGEFORMAT", re.I)),
    ("shape_field_code", re.compile(r"SHAPE[ \t]*\\\*[ \t]*MERGEFORMAT", re.I)),
    ("embed_field_code", re.compile(r"EMBED(?:\s+[A-Za-z0-9_.\-]+)?[ \t]*\\\*[ \t]*MERGEFORMAT", re.I)),
    ("eq_roman_field", re.compile(
        r"=[ \t]*[0-9]+[ \t]*\\\*[ \t]*(?:ROMAN|ARABIC|ALPHA)[ \t]*\\\*[ \t]*MERGEFORMAT", re.I)),
    # 兜底：任何残留的 `\* MERGEFORMAT`（前面 owner 已在上面的规则里被吃掉）
    ("stray_control_code", re.compile(r"[ \t]*\\\*[ \t]*MERGEFORMAT", re.I)),
]

# HYPERLINK 域：`HYPERLINK "url" \t "_blank" \o "提示"` 之后紧跟的是显示文本，要保留。
_HYPERLINK_HEAD = "HYPERLINK"
_SWITCH = re.compile(r"\\+[a-zA-Z]")                         # \l、\\t（至少一个反斜杠）
_QUOTED = re.compile(r'\\*"[^"\n]*\\*"')                      # "url" 或 \"url\"（URL 内不会有引号）
_BARE_TARGET = re.compile(r"[^\"\s\u4e00-\u9fa5]+")           # \l _Toc12660（无中文字符才当目标）

# pandoc 转换留下的锚点标记：[]{#_Toc5760 .anchor}
_TOC_ANCHOR = re.compile(r"\]\(\)\{#_Toc\d+\s+\.anchor\}")
_TOC_ANCHOR2 = re.compile(r"\[\]\{#_Toc\d+\s+\.anchor\}")
# markdown 书签链接：[第二十六条](#_Toc15055) → 只留文字
_TOC_LINK = re.compile(r"\[([^\]\n]*)\]\(#_Toc\d+\)")

# 页脚页码装饰行：`- 12 -`、`2`、`—  `、`24 —`、`－`
_PAGE_LINE = re.compile(r"^[\s\u3000\d—–\-－]+$")
# 页脚行里允许出现的装饰字符：破折号、横线、空格，以及被 OCR 认成「一」的横线
_PAGE_FOOTER_DECOR = re.compile(r"[\s\u3000\u00a0\d—–\-－─一?]")
# 还留着 `\* Arabic` / `\* ArabicDash` 编号开关的页脚行
_PAGE_NUM_SWITCH = re.compile(r"\\\*[ \t]*[A-Za-z]+")


def _is_page_footer_line(line: str) -> bool:
    """判断整行是否只是 Word 页脚残留（PAGE 域 + 页码 + 装饰线）。

    `─ PAGE  1 ─`、`PAGE  - 1 -`、`一— PAGE  1 —一`、`PAGE 6 PAGE 9`
    都是同一类东西：源文件里 `\\* MERGEFORMAT` 已丢失，只剩域名字与页码。
    判据是「含 PAGE，且去掉 PAGE 后整行只剩装饰字符、空格和数字」——
    任何一行真正的条文都不会满足这个条件，所以不会误伤正文。
    """
    s = (line or "").strip()
    if not s or "PAGE" not in s.upper():
        return False
    body = re.sub(r"PAGE", "", s, flags=re.I)
    body = _PAGE_NUM_SWITCH.sub("", body)
    return all(_PAGE_FOOTER_DECOR.fullmatch(ch) for ch in body)


# 被吃掉了控制码、只剩 owner 名（可能还带页码）的孤行：PAGE、PAGE  14
_STRAY_OWNER = re.compile(r"^(PAGE|SHAPE|EMBED|ROMAN|=)\s*\d*$")

# 删除 HYPERLINK 后留下的 `《 `、`「 ` 空格
_SPACE_AFTER_OPEN = re.compile(r"(《|「|（|“)[ \t]{1,3}")

_CJK = re.compile(r"[\u4e00-\u9fa5]")


def _strip_hyperlinks(text: str) -> tuple[str, int, int]:
    """删除 HYPERLINK 域代码前缀，保留其后的显示文本。

    返回 (新文本, 删除的域代码个数, 域代码里包含的中文字符数)。
    URL 与气泡提示里可能直接写着中文（`https://baike.baidu.com/item/中华人民共和国体育法`），
    这些中文属于域代码而不是正文，删掉不算误伤，所以要单独计数。
    """
    if _HYPERLINK_HEAD not in text:
        return text, 0, 0
    out: list[str] = []
    i = 0
    removed = 0
    code_cjk = 0
    while True:
        j = text.find(_HYPERLINK_HEAD, i)
        if j < 0:
            out.append(text[i:])
            break
        out.append(text[i:j])
        k = j + len(_HYPERLINK_HEAD)
        while True:
            while k < len(text) and text[k] in " \t":
                k += 1
            switch = _SWITCH.match(text, k)
            if switch:
                k = switch.end()
                continue
            quoted = _QUOTED.match(text, k)
            if quoted:
                k = quoted.end()
                continue
            bare = _BARE_TARGET.match(text, k)
            if bare:
                k = bare.end()
                continue
            break
        removed += 1
        code_cjk += len(_CJK.findall(text[j:k]))
        i = k
    return "".join(out), removed, code_cjk


def clean_text(text: str) -> tuple[str, list[str], int]:
    """清洗一条正文。返回 (新文本, 命中的规则名, 误伤的中文字符数)。

    `\\o "提示文字"` 是域代码里的气泡提示，与紧后的显示文本重复，删掉它不会丢正文；
    这部分中文字符单列为「可删」，不计入误伤，否则工具会把正常清洗判成数据损坏。
    """
    original = text or ""
    hits: list[str] = []
    touched_page = False
    code_cjk = 0
    # 页脚行里被 OCR 认成横线的「一」属于装饰，删它不算误伤正文
    footer_decor_cjk = 0

    for name, pat in _FIELD_CODES:
        new, n = pat.subn("", original)
        if n:
            hits.append(name)
            original = new
            if name != "stray_control_code":
                touched_page = touched_page or name in ("page_field_code", "shape_field_code")

    new, n, code_cjk = _strip_hyperlinks(original)
    if n:
        hits.append("hyperlink_field")
        # 只对做过 HYPERLINK 删除的行修 `《 ` 这类空格，避免影响无关条文
        new = _SPACE_AFTER_OPEN.sub(r"\1", new)
        original = new

    new, n = _TOC_ANCHOR.subn("", original)
    if n:
        hits.append("toc_anchor")
        original = new
    new, n = _TOC_ANCHOR2.subn("", original)
    if n:
        hits.append("toc_anchor")
        original = new
    new, n = _TOC_LINK.subn(r"\1", original)
    if n:
        hits.append("toc_anchor")
        original = new

    # 只剩 PAGE 与页码的页脚行，任何行都清（源文件里 `\* MERGEFORMAT` 已丢失的那批）
    lines = original.split("\n")
    footer_lines = [ln for ln in lines if _is_page_footer_line(ln)]
    if footer_lines:
        footer_decor_cjk = sum(len(_CJK.findall(ln)) for ln in footer_lines)
        original = "\n".join(ln for ln in lines if not _is_page_footer_line(ln))
        hits.append("page_footer_line")

    if touched_page:
        # 只在动过 PAGE 域的行上清页码装饰，防止把普通条文里的纯数字行误删
        lines = original.split("\n")
        kept = [ln for ln in lines
                if not _PAGE_LINE.match(ln.strip()) or not ln.strip()]
        kept = [ln for ln in kept if not _STRAY_OWNER.match(ln.strip())]
        if len(kept) != len(lines):
            hits.append("page_footer_line")
            original = "\n".join(kept)

    cleaned = "\n".join(ln.rstrip() for ln in original.split("\n")).strip()
    removed_cjk = len(_CJK.findall(text or "")) - len(_CJK.findall(cleaned))
    return cleaned, hits, max(0, removed_cjk - code_cjk - footer_decor_cjk)


def _has_residue(text: str) -> bool:
    t = text or ""
    return ("MERGEFORMAT" in t.upper() or "HYPERLINK" in t
            or "_Toc" in t or "PAGE" in t.upper())


def residue_tokens() -> dict[str, re.Pattern]:
    """清洗后应该一个都不剩的特征串，用于交付前自证。"""
    return {
        "MERGEFORMAT": re.compile(r"MERGEFORMAT", re.I),
        "HYPERLINK": re.compile(r"\bHYPERLINK\b"),
        "_Toc": re.compile(r"_Toc\d+"),
        "javascript": re.compile(r"javascript:", re.I),
    }


def _context(text: str, start: int, end: int, pad: int = 30) -> str:
    s = max(0, start - pad)
    e = min(len(text), end + pad)
    return text[s:e].replace("\n", " ⏎ ")


def scan(conn: sqlite3.Connection, sample_cap: int) -> dict:
    """扫描全库，返回统计数据与样本（不写库）。"""
    stats: dict[str, dict] = {}
    for name, _ in _FIELD_CODES:
        stats[name] = {"rows": 0, "laws": set(), "chars": 0}
    for name in ("hyperlink_field", "toc_anchor", "page_footer_line"):
        stats[name] = {"rows": 0, "laws": set(), "chars": 0}

    samples: list[dict] = []
    touched_rows = 0
    touched_laws: set[str] = set()
    chars_removed = 0
    cjk_removed = 0
    emptied = 0
    remaining = Counter()
    scans = 0

    for row in conn.execute(
            "SELECT id, law_name, article_number, article, content FROM law_articles"):
        scans += 1
        old_content = row["content"] or ""
        old_article = row["article"] or ""
        if not _has_residue(old_content) and not _has_residue(old_article):
            continue

        new_content, hits_c, cjk_c = clean_text(old_content)
        new_article, hits_a, cjk_a = clean_text(old_article)
        hits = list(dict.fromkeys(hits_c + hits_a))
        if not hits:
            continue

        touched_rows += 1
        touched_laws.add(row["law_name"])
        delta = len(old_content) - len(new_content) + len(old_article) - len(new_article)
        chars_removed += delta
        cjk_removed += cjk_c + cjk_a
        if not new_content.strip():
            emptied += 1
        for h in hits:
            stats[h]["rows"] += 1
            stats[h]["laws"].add(row["law_name"])
            stats[h]["chars"] += delta if len(hits) == 1 else 0
        for tok, pat in residue_tokens().items():
            if pat.search(new_content) or pat.search(new_article):
                remaining[tok] += 1

        if len(samples) < sample_cap:
            samples.append({
                "id": row["id"],
                "law_name": row["law_name"],
                "article_number": row["article_number"],
                "rules": hits,
                "before": old_content[-300:] if len(old_content) > 300 else old_content,
                "after": new_content[-300:] if len(new_content) > 300 else new_content,
                "chars_removed": delta,
            })

    for name, s in stats.items():
        s["laws"] = len(s["laws"])
    return {
        "scanned_articles": scans,
        "touched_rows": touched_rows,
        "touched_laws": len(touched_laws),
        "chars_removed": chars_removed,
        "cjk_removed": cjk_removed,
        "emptied_after_clean": emptied,
        "residue_remaining": dict(remaining),
        "rules": stats,
        "samples": samples,
    }


def apply_clean(conn: sqlite3.Connection, dry_run: bool) -> dict:
    """全库清洗。dry_run=True 时只统计不提交。"""
    summary = {"touched_rows": 0, "chars_removed": 0, "cjk_removed": 0,
               "emptied": 0, "rules": Counter()}
    try:
        for row in conn.execute(
                "SELECT id, article, content FROM law_articles"):
            old_content = row["content"] or ""
            old_article = row["article"] or ""
            if not _has_residue(old_content) and not _has_residue(old_article):
                continue
            new_content, hits_c, cjk_c = clean_text(old_content)
            new_article, hits_a, cjk_a = clean_text(old_article)
            hits = list(dict.fromkeys(hits_c + hits_a))
            if not hits:
                continue
            if not new_content.strip():
                summary["emptied"] += 1
                continue
            summary["touched_rows"] += 1
            summary["chars_removed"] += (len(old_content) - len(new_content)
                                         + len(old_article) - len(new_article))
            summary["cjk_removed"] += cjk_c + cjk_a
            for h in hits:
                summary["rules"][h] += 1
            if not dry_run:
                conn.execute(
                    "UPDATE law_articles SET content = ?, article = ?, content_hash = ? WHERE id = ?",
                    (new_content, new_article, content_hash(new_content), row["id"]))
        if summary["cjk_removed"] > 0:
            # 中文被删 = 误伤，整体回滚，库保持原样
            raise RuntimeError(f"误伤中文字符 {summary['cjk_removed']} 个，已回滚")
        if not dry_run:
            create_fts(conn)
            summary["new_corpus_version"] = bump_corpus_version(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    summary["rules"] = dict(summary["rules"])
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="清洗 Word 域代码残渣")
    ap.add_argument("--db", default="./data/law_data.db")
    ap.add_argument("--apply", action="store_true", help="实洗并写库（默认只干跑）")
    ap.add_argument("--report", default=None, help="报告 JSON 输出路径")
    ap.add_argument("--samples", type=int, default=12, help="取样条数")
    args = ap.parse_args()

    if not os.path.isfile(args.db):
        raise SystemExit(f"库不存在：{args.db}")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    before_version = meta_get(conn, "corpus_version", "0")
    report = {"db": os.path.abspath(args.db), "dry_run": not args.apply,
              "corpus_version_before": before_version}

    if args.apply:
        result = apply_clean(conn, dry_run=False)
        report["apply"] = result
        report["corpus_version_after"] = result.get("new_corpus_version")
    else:
        report["scan"] = scan(conn, args.samples)
        report["apply_preview"] = apply_clean(conn, dry_run=True)
        conn.rollback()

    conn.close()

    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.report:
        os.makedirs(os.path.dirname(os.path.abspath(args.report)), exist_ok=True)
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"报告已写入 {args.report}")
    else:
        print(text)


if __name__ == "__main__":
    main()
