# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""清掉修改决定正文开头那行解析标注。

早先 tools/fetch_mirror.py 在生成修改决定的 Markdown 时，会在「第1条」后面
硬加一句「（修改决定全文，共 N 项修改内容，项号以「一、二、三」排列）」。
那是给抓取排查用的定位说明，通用解析器照字面收进了 content —— 于是库里
每一部修改决定的正文都以这行人为文字开头。检索会命中它，律师复制条文到
文书里也会带上它，等于给法条凭空加了一句原文没有的话。

生成端已在 fetch_mirror.py 改掉（只写「第1条」），本脚本负责清存量。

默认只出报告，--apply 才写库。改了 content 就要同步重算 content_hash，
并重建 trigram 全文索引，否则检索命中的还是旧文本。

用法：
  python3 tools/strip_amendment_note.py                 # 只报告
  python3 tools/strip_amendment_note.py --apply         # 写库并重建索引
  python3 tools/strip_amendment_note.py --apply --no-fts
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "ingest"))

from schema import content_hash, create_fts  # noqa: E402

# 标注固定在最前面，单独成行；括号是全角，数字是「N 项」的阿拉伯数字。
NOTE_RE = re.compile(r"^（修改决定全文，共\s*\d+\s*项修改内容[^）]*）\s*\n?")


def strip_note(content: str) -> str:
    """去掉开头标注。没有标注的原样返回。"""
    return NOTE_RE.sub("", content, count=1)


def _flatten(s: str) -> str:
    """去掉换行转义符与全角空格，用于判断某行是不是标题的一部分。"""
    return s.replace("\\", "").replace("\u3000", "").replace(" ", "").strip()


def strip_title_fragment(content: str, law_name: str) -> tuple[str, int]:
    """删掉正文开头残留的标题行。

    解析器把法规标题当成正文收进来了，于是很多修改决定的 content 以标题残片
    开头，形如「《海南省环境保护条例》的决定」或跨行截断的「关于废止和修改
    《昆明市九乡风景名胜区\\」+「保护条例》等 13 件地方性法规的决定」。
    判定依据是「这一行是法规名的子串」——标题必然包含于法规名，而真正的修改
    条文不会整行都是法规名的一截。跨行残片要连着删，所以用循环。

    两道保险：删完不能为空；到底了要留最后一行。

    长度下限取 3 而不是 6：标题跨多行时，末尾那截常常只剩「的决定」三个字，
    卡 6 字会把它留下，正文就以「的决定」开头了。
    """
    norm = _flatten(law_name)
    lines = content.split("\n")
    removed = 0
    while len(lines) > 1:
        head = _flatten(lines[0])
        if not head:                 # 空行直接吃掉
            lines.pop(0)
            continue
        if len(head) >= 3 and head in norm:
            lines.pop(0)
            removed += 1
            continue
        break
    return "\n".join(lines), removed


def main() -> None:
    ap = argparse.ArgumentParser(description="清掉修改决定正文开头的解析残留")
    ap.add_argument("--db", default="./data/law_data.db")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只出报告）")
    ap.add_argument("--no-fts", action="store_true", help="不重建全文索引")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 条（试跑用）")
    ap.add_argument("--title", action="store_true", help="同时清掉正文开头的标题残片")
    ap.add_argument("--show", type=int, default=8, help="抽样显示条数")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit(f"库不存在：{args.db}")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT a.id, a.law_name, a.article_number, a.content, a.content_hash "
        "FROM law_articles a JOIN law_versions v ON v.id = a.version_id "
        "WHERE v.law_type_label = '修改决定' ORDER BY a.id"
    ).fetchall()
    if args.limit:
        rows = rows[: args.limit]

    if not rows:
        print("库里没有修改决定的条文。")
        conn.close()
        return

    print(f"修改决定条文 {len(rows)} 条，涉及 {len({r['law_name'] for r in rows})} 部法规")
    print(f"清理项：标注{'＋标题残片' if args.title else '（标题残片未启用，加 --title）'}\n")

    changes = []
    n_note = n_title = 0
    for r in rows:
        new = strip_note(r["content"])
        cut_note = len(r["content"]) - len(new)
        cut_title = 0
        if args.title:
            new, cut_title = strip_title_fragment(new, r["law_name"])
        if not new.strip():
            continue                       # 删空了，宁可不碰
        if new == r["content"]:
            continue                       # 本来就干净
        n_note += 1 if cut_note else 0
        n_title += 1 if cut_title else 0
        changes.append((new, content_hash(new), r["id"], r["law_name"],
                        r["article_number"], len(r["content"]) - len(new)))

    print(f"需要清理 {len(changes)} 条（含标注 {n_note} 条，含标题残片 {n_title} 条）")

    if changes:
        print(f"\n抽样（前 {args.show} 条，-N 为删掉的字符数）：")
        for _, _, aid, name, num, cut in changes[: args.show]:
            print(f"  id={aid} {name[:34]:36} {num}  -{cut}")

    if not args.apply:
        print("\n（只出报告。确认无误后加 --apply 写库。）")
        conn.close()
        return

    with conn:
        conn.executemany(
            "UPDATE law_articles SET content=?, content_hash=? WHERE id=?",
            [(c[0], c[1], c[2]) for c in changes],
        )
    print(f"\n已更新 {len(changes)} 条正文与 content_hash")

    if not args.no_fts:
        print("重建全文索引…")
        ok = create_fts(conn)
        print("  索引已重建" if ok else "  当前 SQLite 不支持 trigram FTS，跳过（服务端会降级 LIKE）")

    conn.close()


if __name__ == "__main__":
    main()
