# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟

"""条文级差异比对 + 变更报告（HTML / JSON）。

比对粒度：以 (条号, 增补后缀) 为主键，逐条判断新增 / 修改 / 删除。
用法（单独跑，只看差异不落库）：
  python tools/diff_articles.py --inbox inbox/
"""
from __future__ import annotations

import argparse
import difflib
import html
import json
import os
import re
import sqlite3
import sys


_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "ingest"))
sys.path.insert(0, _HERE)

from parse_markdown import parse_markdown_file  # noqa: E402
from schema import (  # noqa: E402
    article_key,
    content_hash,
    ensure_schema,
    file_sha256,
    latest_version,
    norm_law_name,
    now_iso,
    split_suffix,
)

DEFAULT_DB = "./data/law_data.db"
CACHE_NAME = re.compile(r"^~\$")


def load_inbox(inbox: str) -> dict[str, dict]:
    """扫目录里的 .md（docx 需先转换），返回 {norm_name: record}。"""
    records: dict[str, dict] = {}
    for dirpath, _dirs, files in os.walk(inbox):
        label = os.path.basename(dirpath)
        for name in sorted(files):
            if not name.lower().endswith(".md") or CACHE_NAME.match(name):
                continue
            path = os.path.join(dirpath, name)
            result = parse_markdown_file(path, label)
            if not result.law_name or not result.articles:
                print(f"  跳过（无法解析）: {parcel(path)}")
                continue
            articles = []
            for art in result.articles:
                suffix, content = split_suffix(art.content)
                articles.append(
                    {
                        "key": article_key(art.article_number_num, suffix),
                        "law_name": result.law_name,
                        "law_type": result.law_type,
                        "law_type_label": result.law_type_label,
                        "article_number": art.article_number + suffix,
                        "article_number_num": art.article_number_num,
                        "article_suffix": suffix,
                        "chapter": art.chapter,
                        "section": art.section,
                        "content": content,
                        "content_hash": content_hash(content),
                        "publish_date": result.publish_date,
                        "effective_date": result.effective_date,
                        "issuing_authority": result.issuing_authority,
                        "source_file": result.source_file,
                    }
                )
            # 同一条号切出两份时保留正文更长的那份：正文里以「第X条」开头的行
            # （引用别的法条、决定里的修改项）会被误切成新条，造成重号。
            # 入库时 ux_articles_key 会让后写入的覆盖前一条，条数就和版本记录对不上。
            dedup: dict[str, dict] = {}
            for a in articles:
                prev = dedup.get(a["key"])
                if prev is None or len(a["content"]) > len(prev["content"]):
                    dedup[a["key"]] = a
            articles = list(dedup.values())
            records[norm_law_name(result.law_name)] = {
                "law_name": result.law_name,
                "norm_name": norm_law_name(result.law_name),
                "law_type": result.law_type,
                "law_type_label": result.law_type_label,
                "publish_date": result.publish_date,
                "effective_date": result.effective_date,
                "issuing_authority": result.issuing_authority,
                "source_file": result.source_file,
                "source_path": path,
                "sha256": file_sha256(path),
                "articles": articles,
            }
    return records


def parcel(path: str) -> str:
    return os.path.relpath(path)


def load_current(conn: sqlite3.Connection, norm_name: str) -> tuple[sqlite3.Row | None, dict]:
    version = latest_version(conn, norm_name)
    if version is None:
        return None, {}
    rows = conn.execute(
        """SELECT article_number_num, article_suffix, article_number, content, content_hash,
                  chapter, section
           FROM law_articles WHERE version_id = ?""",
        (version["id"],),
    ).fetchall()
    return version, {article_key(r["article_number_num"], r["article_suffix"] or ""): r for r in rows}


def diff_law(version, old_map: dict, record: dict) -> dict:
    new_map = {a["key"]: a for a in record["articles"]}
    added = [new_map[k] for k in new_map if k not in old_map]
    removed = [
        {"key": k, "article_number": v["article_number"], "old": v["content"]}
        for k, v in old_map.items()
        if k not in new_map
    ]
    changed = []
    for key, new in new_map.items():
        old = old_map.get(key)
        if old is None:
            continue
        if (old["content_hash"] or "") == new["content_hash"]:
            continue
        changed.append(
            {
                "key": key,
                "article_number": new["article_number"],
                "old": old["content"],
                "new": new["content"],
            }
        )
    is_new_law = version is None
    same_or_older = (
        version is not None
        and (record.get("publish_date") or "") <= (version["publish_date"] or "")
    )
    # 新文件比库里还旧 → 单独标 stale，绝不覆盖。
    # 镜像里存在同一部法的历史文本（例如北京市大气污染防治条例，镜像给的是 2014 版，
    # 库里已经是 2018 版）。原逻辑是「只要条文有变化就算 update」，会拿旧文本顶掉新版本，
    # 检索结果会整体倒退。
    strictly_older = (
        version is not None
        and (record.get("publish_date") or "") < (version["publish_date"] or "")
    )
    if is_new_law:
        action = "new"
    elif strictly_older:
        action = "stale"
    elif same_or_older and not changed:
        action = "skip"
    else:
        action = "update"
    return {
        "action": action,
        "law_name": record["law_name"],
        "norm_name": record["norm_name"],
        "publish_date": record["publish_date"],
        "effective_date": record["effective_date"],
        # 以下三个字段原先没往计划里带，写库时 insert_new_version 取 law.get("law_type")
        # 一律取到 None，于是版本表的法规类型全空（条文明细里却是有值的）。
        # 前端按类型筛选、/health 的类型统计都会因此漏掉这次新入库的法规。
        "law_type": record.get("law_type"),
        "law_type_label": record.get("law_type_label"),
        "issuing_authority": record.get("issuing_authority"),
        "source_file": record["source_file"],
        "sha256": record["sha256"],
        "old_version_id": version["id"] if version else None,
        "old_publish_date": version["publish_date"] if version else None,
        "articles": record["articles"],
        "added": added,
        "changed": changed,
        "removed": removed,
        "stats": {
            "total": len(new_map),
            "added": len(added),
            "changed": len(changed),
            "removed": len(removed),
        },
    }


def build_plan(db_path: str, inbox: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    records = load_inbox(inbox)
    laws = []
    for norm, record in sorted(records.items()):
        version, old_map = load_current(conn, norm)
        laws.append(diff_law(version, old_map, record))
    corpus_version = conn.execute(
        "SELECT value FROM corpus_meta WHERE key='corpus_version'"
    ).fetchone()
    conn.close()
    return {
        "schema": 1,
        "generated_at": now_iso(),
        "base_corpus_version": corpus_version[0] if corpus_version else "0",
        "inbox": inbox,
        "laws": laws,
        "summary": {
            "files": len(laws),
            "new_laws": sum(1 for x in laws if x["action"] == "new"),
            "updated_laws": sum(1 for x in laws if x["action"] == "update"),
            "skipped_laws": sum(1 for x in laws if x["action"] == "skip"),
            "stale_laws": sum(1 for x in laws if x["action"] == "stale"),
            "added_articles": sum(x["stats"]["added"] for x in laws),
            "changed_articles": sum(x["stats"]["changed"] for x in laws),
            "removed_articles": sum(x["stats"]["removed"] for x in laws),
        },
    }


def _diff_html(old: str, new: str) -> str:
    rows = []
    matcher = difflib.SequenceMatcher(None, old, new)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            rows.append(f"<span>{html.escape(old[i1:i2])}</span>")
        elif tag == "delete":
            rows.append(f"<del>{html.escape(old[i1:i2])}</del>")
        elif tag == "insert":
            rows.append(f"<ins>{html.escape(new[j1:j2])}</ins>")
        else:
            rows.append(f"<del>{html.escape(old[i1:i2])}</del><ins>{html.escape(new[j1:j2])}</ins>")
    return "".join(rows)


def render_html(plan: dict) -> str:
    s = plan["summary"]
    parts = [
        """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>法规变更报告</title>
<style>
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#17293F;background:#F5F7FA;margin:0;padding:32px 24px;line-height:1.7}
.wrap{max-width:920px;margin:0 auto}
h1{font-size:22px;margin:0 0 6px;font-weight:600}
.sub{color:#5D6B7A;font-size:13px;margin-bottom:22px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:26px}
.card{background:#fff;border:1px solid #E2E8F0;border-radius:10px;padding:12px 18px;min-width:110px}
.card b{display:block;font-size:20px;font-weight:600}
.card span{font-size:12px;color:#5D6B7A}
.law{background:#fff;border:1px solid #E2E8F0;border-radius:10px;margin-bottom:16px;overflow:hidden}
.law header{padding:12px 18px;border-bottom:1px solid #E2E8F0;display:flex;justify-content:space-between;align-items:baseline;gap:12px}
.law h2{font-size:15px;margin:0;font-weight:600}
.tag{font-size:12px;padding:2px 8px;border-radius:999px;white-space:nowrap}
.tag.new{background:#E1F5EE;color:#0F6E56}
.tag.update{background:#FAEEDA;color:#854F0B}
.tag.skip{background:#F1F3F5;color:#5D6B7A}
.tag.stale{background:#FCEBEB;color:#A32D2D}
.new{background:#E1F5EE;color:#0F6E56}.update{background:#FAEEDA;color:#854F0B}.skip{background:#F1EFE8;color:#5F5E5A}
.row{padding:12px 18px;border-top:1px solid #F0F2F5;font-size:13px}
.row:first-of-type{border-top:none}
.k{font-weight:600;margin-right:8px}
.type{font-size:11px;padding:1px 6px;border-radius:4px;margin-left:6px}
.a-add{background:#E1F5EE;color:#0F6E56}.a-chg{background:#FAEEDA;color:#854F0B}.a-del{background:#FCEBEB;color:#A32D2D}
del{background:#FCEBEB;color:#A32D2D;text-decoration:line-through}
ins{background:#E1F5EE;color:#0F6E56;text-decoration:none}
.meta{color:#5D6B7A;font-size:12px}
footer{color:#5D6B7A;font-size:12px;margin-top:26px}
</style></head><body><div class="wrap">""",
        f"<h1>法规变更报告</h1><div class='sub'>生成时间 {plan['generated_at']} ｜ 基准库版本 {plan['base_corpus_version']} ｜ 扫描目录 {html.escape(plan['inbox'])}</div>",
        "<div class='cards'>",
        f"<div class='card'><b>{s['files']}</b><span>待处理文件</span></div>",
        f"<div class='card'><b>{s['new_laws']}</b><span>新法规</span></div>",
        f"<div class='card'><b>{s['updated_laws']}</b><span>有修订</span></div>",
        f"<div class='card'><b>{s['added_articles']}</b><span>新增条文</span></div>",
        f"<div class='card'><b>{s['changed_articles']}</b><span>修改条文</span></div>",
        f"<div class='card'><b>{s['removed_articles']}</b><span>删除条文</span></div>",
        f"<div class='card'><b>{s.get('stale_laws', 0)}</b><span>镜像版本更旧·不采用</span></div>",
        "</div>",
    ]
    label = {"new": "新法规", "update": "有修订", "skip": "无变化/旧文件",
             "stale": "镜像版本更旧·不采用"}
    for law in plan["laws"]:
        st = law["stats"]
        parts.append("<section class='law'><header>")
        parts.append(f"<h2>{html.escape(law['law_name'])}</h2>")
        parts.append(
            f"<span class='tag {law['action']}'>{label[law['action']]}"
            f"　+{st['added']} 改{st['changed']} -{st['removed']}</span>"
        )
        parts.append("</header>")
        parts.append(
            f"<div class='row meta'>文件 {html.escape(law['source_file'])} ｜ "
            f"公布 {law['publish_date'] or '—'} ｜ 施行 {law['effective_date'] or '—'} ｜ "
            f"现有版本 {law['old_publish_date'] or '无'} ｜ 条文 {st['total']} 条</div>"
        )
        for art in law["changed"]:
            parts.append(
                f"<div class='row'><span class='k'>{html.escape(art['article_number'])}</span>"
                f"<span class='type a-chg'>修改</span><br>{_diff_html(art['old'], art['new'])}</div>"
            )
        for art in law["added"]:
            parts.append(
                f"<div class='row'><span class='k'>{html.escape(art['article_number'])}</span>"
                f"<span class='type a-add'>新增</span><br>{html.escape(art['content'])}</div>"
            )
        for art in law["removed"]:
            parts.append(
                f"<div class='row'><span class='k'>{html.escape(art['article_number'])}</span>"
                f"<span class='type a-del'>删除</span><br><del>{html.escape(art['old'])}</del></div>"
            )
        if law["action"] == "skip":
            parts.append("<div class='row meta'>内容与库内版本一致，确认后跳过。</div>")
        parts.append("</section>")
    parts.append(
        "<footer>确认无误后执行：python tools/update.py --apply &lt;本报告同目录 .plan.json&gt; --approve</footer>"
        "</div></body></html>"
    )
    return "".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser(description="只出差异报告，不写库")
    ap.add_argument("--inbox", default="./inbox")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--out-dir", default="./reports")
    args = ap.parse_args()

    plan = build_plan(args.db, args.inbox)
    os.makedirs(args.out_dir, exist_ok=True)
    stamp = plan["generated_at"].replace(":", "").replace("-", "")[:13]
    plan_path = os.path.join(args.out_dir, f"changes-{stamp}.plan.json")
    html_path = os.path.join(args.out_dir, f"changes-{stamp}.html")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(plan))
    s = plan["summary"]
    print(f"报告：{html_path}")
    print(f"计划：{plan_path}")
    print(f"  新法规 {s['new_laws']} ｜ 修订 {s['updated_laws']} ｜ 无变化 {s['skipped_laws']} ｜ "
          f"新增条 {s['added_articles']} ｜ 修改条 {s['changed_articles']} ｜ 删除条 {s['removed_articles']}")


if __name__ == "__main__":
    main()
