# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""从法规 Markdown 直接构建 SQLite 真源库（反转主从：不再依赖 ChromaDB）。

用法：
  python ingest/build_sqlite_from_markdown.py --markdown-dir ./inbox_md
  python ingest/build_sqlite_from_markdown.py --markdown-dir ./法规库 --keep-history

默认同一部法只保留公布日期最新的版本；--keep-history 会保留旧版本
（旧版本 status=superseded，检索默认不返回，可用 include_history 查询）。
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from parse_markdown import parse_markdown_file  # noqa: E402
from schema import (  # noqa: E402
    content_hash,
    create_fts,
    ensure_schema,
    file_sha256,
    norm_law_name,
    now_iso,
    rebuild_bigram_index,
    split_suffix,
    bump_corpus_version,
)

DEFAULT_MARKDOWN_DIR = "./inbox_md"
DEFAULT_OUTPUT = "./data/law_data.db"


def iter_markdown(root: str):
    for dirpath, _dirnames, filenames in os.walk(root):
        dir_label = os.path.basename(dirpath)
        for name in sorted(filenames):
            if name.lower().endswith(".md"):
                yield os.path.join(dirpath, name), dir_label


def collect(markdown_dir: str):
    """返回 ({norm_name: [file_record, ...]}, 多份文件的法规数)。每部法按公布日期升序。"""
    by_law: dict[str, list[dict]] = defaultdict(list)
    for path, dir_label in iter_markdown(markdown_dir):
        result = parse_markdown_file(path, dir_label)
        if not result.law_name or not result.articles:
            print(f"  跳过（无法解析）: {path}")
            continue
        articles = []
        for art in result.articles:
            suffix, content = split_suffix(art.content)
            articles.append(
                {
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
        by_law[norm_law_name(result.law_name)].append(
            {
                "law_name": result.law_name,
                "law_type": result.law_type,
                "law_type_label": result.law_type_label,
                "publish_date": result.publish_date,
                "effective_date": result.effective_date,
                "issuing_authority": result.issuing_authority,
                "source_file": result.source_file,
                "sha256": file_sha256(path),
                "articles": articles,
            }
        )
    multi = 0
    for files in by_law.values():
        # 排序即「新旧判定」：公布日期升序，最后一份被当作最新版（status=effective）。
        # 同日或无日期时，让元数据更完整、条文更多的那份排后面：源数据里同一部法
        # 常有两份转换件（一份名称带尾下划线、无公布日期），不能让它们互相当版本，
        # 更不能两条都标成现行有效。
        files.sort(
            key=lambda f: (
                f["publish_date"] or "",
                len(f["articles"]),
                1 if f["law_type_label"] else 0,
                f["source_file"],
            )
        )
        if len(files) > 1:
            multi += 1
    return by_law, multi


def insert_version(conn: sqlite3.Connection, record: dict, status: str, batch_id: str,
                   repeal_date: str = "") -> int:
    cur = conn.execute(
        """INSERT INTO law_versions
           (law_name, norm_name, law_type, law_type_label, publish_date, effective_date,
            issuing_authority, source_file, file_sha256, article_count, status, batch_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            record["law_name"],
            norm_law_name(record["law_name"]),
            record["law_type"],
            record["law_type_label"],
            record["publish_date"],
            record["effective_date"],
            record["issuing_authority"],
            record["source_file"],
            record["sha256"],
            len(record["articles"]),
            status,
            batch_id,
            now_iso(),
        ),
    )
    version_id = int(cur.lastrowid)
    conn.executemany(
        """INSERT OR REPLACE INTO law_articles
           (vector_id, version_id, law_name, law_type, law_type_label, article_number,
            article_number_num, article_suffix, chapter, section, article, content, content_hash,
            publish_date, effective_date, issuing_authority, status, repeal_date, source_file)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                f"{record['source_file']}|{a['article_number_num']}{a['article_suffix']}",
                version_id,
                a["law_name"],
                a["law_type"],
                a["law_type_label"],
                a["article_number"],
                a["article_number_num"],
                a["article_suffix"],
                a["chapter"],
                a["section"],
                a["content"],
                a["content"],
                a["content_hash"],
                a["publish_date"],
                a["effective_date"],
                a["issuing_authority"],
                # 旧版本条文必须标 repealed，否则会被当成现行法条检索出来
                "repealed" if status == "superseded" else "effective",
                repeal_date,
                a["source_file"],
            )
            for a in record["articles"]
        ],
    )
    return version_id


def build(markdown_dir: str, output: str, keep_history: bool, replace: bool) -> dict:
    if os.path.exists(output) and not replace:
        raise SystemExit(f"目标库已存在：{output}（如需覆盖请加 --replace）")
    if os.path.exists(output) and replace:
        os.remove(output)

    os.makedirs(os.path.dirname(os.path.abspath(output)), exist_ok=True)
    conn = sqlite3.connect(output)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    batch_id = f"build-{now_iso()}"
    conn.execute("INSERT INTO ingest_batches(id, started_at, note) VALUES (?,?,?)",
                 (batch_id, now_iso(), "build_sqlite_from_markdown"))

    by_law, multi_files = collect(markdown_dir)
    total_laws = total_articles = skipped_versions = 0
    for _norm, files in by_law.items():
        chosen = files if keep_history else files[-1:]
        skipped_versions += len(files) - len(chosen)
        for idx, record in enumerate(chosen):
            is_latest = idx == len(chosen) - 1
            status = "effective" if is_latest else "superseded"
            repeals_on = "" if is_latest else (chosen[idx + 1]["effective_date"] or "")
            insert_version(conn, record, status, batch_id, repeals_on)
            total_articles += len(record["articles"])
        total_laws += 1

    # 旧版本标明被哪个版本取代
    conn.execute(
        "UPDATE law_versions SET superseded_by = ("
        "  SELECT MAX(v2.id) FROM law_versions v2"
        "  WHERE v2.norm_name = law_versions.norm_name AND v2.status = 'effective'"
        ") WHERE status = 'superseded'"
    )

    # 只保留最新版本时，把旧版本连同条文一并清掉（循环外做一次，避免逐部全表扫描）
    if not keep_history:
        conn.execute(
            "DELETE FROM law_articles WHERE version_id NOT IN "
            "(SELECT id FROM law_versions WHERE status='effective')"
        )
        conn.execute("DELETE FROM law_versions WHERE status != 'effective'")

    conn.execute(
        "UPDATE ingest_batches SET finished_at=?, file_count=?, added_laws=?, added_articles=? WHERE id=?",
        (now_iso(), sum(len(f) for f in by_law.values()), total_laws, total_articles, batch_id),
    )
    create_fts(conn)
    # 全量建库是二字索引唯一的重建时机：FTS5 trigram 只切 3 字窗口，
    # 2 字查询没有这本倒排就只能全表 LIKE（实测 2.8–3.9 秒）。
    bigram_stats = rebuild_bigram_index(conn)
    version = bump_corpus_version(conn)
    stats = conn.execute("SELECT COUNT(*) FROM law_articles").fetchone()[0]
    laws = conn.execute("SELECT COUNT(DISTINCT norm_name) FROM law_versions").fetchone()[0]
    conn.commit()
    conn.close()
    return {
        "corpus_version": version,
        "laws": laws,
        "articles": stats,
        "bigram": bigram_stats,
        "skipped_versions": skipped_versions,
        "multi_files": multi_files,
        "output": output,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="从法规 Markdown 构建 SQLite 真源库")
    parser.add_argument("--markdown-dir", default=DEFAULT_MARKDOWN_DIR)
    parser.add_argument("--output-path", default=DEFAULT_OUTPUT)
    parser.add_argument("--keep-history", action="store_true", help="保留旧版本条文")
    parser.add_argument("--replace", action="store_true", help="覆盖已有库")
    args = parser.parse_args()

    if not os.path.isdir(args.markdown_dir):
        raise SystemExit(f"目录不存在：{args.markdown_dir}")
    info = build(args.markdown_dir, args.output_path, args.keep_history, args.replace)
    print(f"完成：{info['output']}")
    print(f"  corpus_version = {info['corpus_version']}")
    print(f"  法规 {info['laws']} 部，法条 {info['articles']} 条")
    big = info["bigram"]
    print(f"  二字索引 {big['grams']} 个词、{big['postings']} 条 posting，耗时 {big['seconds']} 秒")
    if info["multi_files"]:
        print(f"  有 {info['multi_files']} 部法规存在多份文件（历史版本或重复转换件）")
    if info["skipped_versions"]:
        print(f"  跳过历史版本 {info['skipped_versions']} 份（只保留每部法规最新版，加 --keep-history 可保留）")


if __name__ == "__main__":
    main()
