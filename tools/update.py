# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""法规增量更新主程序：扫描 → 转换 → 解析 → 比对 → 报告 →（人工确认）→ 写库 → 索引 → 校验。

用法：
  # 第一步：只出报告，不写库（可以反复跑，幂等）
  python tools/update.py --inbox inbox/

  # 人工看报告后确认应用
  python tools/update.py --apply reports/changes-20261030T0250.plan.json --approve

  # 顺带重建本次涉及法规的向量索引（需已安装 chromadb + sentence-transformers）
  python tools/update.py --apply reports/xxx.plan.json --approve --vectors
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path


_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "ingest"))
sys.path.insert(0, _HERE)

from diff_articles import build_plan, render_html  # noqa: E402
from schema import (  # noqa: E402
    STATUS_PENDING,
    create_fts,
    ensure_schema,
    initial_status,
    meta_get,
    meta_set,
    now_iso,
    norm_law_name,
    bump_corpus_version,
)

DEFAULT_DB = "./data/law_data.db"
DEFAULT_INBOX = "./inbox"
REPORT_DIR = os.environ.get("LAW_REPORT_DIR", "./reports")


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def convert_inbox(inbox: str) -> list[str]:
    """把 inbox 里的 doc/docx 转成 md，输出到 inbox/_converted/。返回转换出的 md 路径。"""
    srcs = [p for p in Path(inbox).rglob("*") if p.suffix.lower() in (".doc", ".docx")
            and not p.name.startswith("~$")]
    if not srcs:
        return []
    out_dir = Path(inbox) / "_converted"
    out_dir.mkdir(parents=True, exist_ok=True)
    pandoc, soffice = _which("pandoc"), _which("soffice") or _which("libreoffice")
    made: list[str] = []
    for src in srcs:
        target = out_dir / (src.stem + ".md")
        if target.exists() and target.stat().st_mtime >= src.stat().st_mtime:
            made.append(str(target))
            continue
        try:
            if src.suffix.lower() == ".docx" and pandoc:
                subprocess.run([pandoc, str(src), "-t", "markdown", "-o", str(target)],
                               check=True, capture_output=True)
            elif soffice:
                subprocess.run([soffice, "--headless", "--convert-to", "docx",
                                "--outdir", str(out_dir), str(src)],
                               check=True, capture_output=True, timeout=180)
                converted = out_dir / (src.stem + ".docx")
                if pandoc and converted.exists():
                    subprocess.run([pandoc, str(converted), "-t", "markdown", "-o", str(target)],
                                   check=True, capture_output=True)
                else:
                    raise RuntimeError("缺少 pandoc，无法把 docx 转成 markdown")
            else:
                raise RuntimeError("未检测到 pandoc / libreoffice，无法转换 Word")
            if target.exists():
                made.append(str(target))
                print(f"  转换完成: {src.name} → {target.name}")
        except Exception as exc:  # noqa: BLE001
            print(f"  [跳过] {src.name}: {exc}")
    return made


def insert_new_version(conn: sqlite3.Connection, law: dict, batch_id: str) -> int:
    # 施行日在今天之后的标 pending，不能一律写 effective。
    # 源数据里已经出现「公布于 2026-08、2027-01-01 才施行」的法，
    # 一律标现行有效等于让人提前引用未生效条文。
    version_status = initial_status(law.get("effective_date"))
    cur = conn.execute(
        """INSERT INTO law_versions
           (law_name, norm_name, law_type, law_type_label, publish_date, effective_date,
            issuing_authority, source_file, file_sha256, article_count, status, batch_id, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            law["law_name"], law["norm_name"], law.get("law_type"), law.get("law_type_label"),
            law["publish_date"], law["effective_date"], law.get("issuing_authority"),
            law["source_file"], law["sha256"], len(law["articles"]), version_status, batch_id, now_iso(),
        ),
    )
    version_id = int(cur.lastrowid)
    conn.executemany(
        """INSERT OR REPLACE INTO law_articles
           (vector_id, version_id, law_name, law_type, law_type_label, article_number,
            article_number_num, article_suffix, chapter, section, article, content, content_hash,
            publish_date, effective_date, issuing_authority, status, source_file)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                f"{law['source_file']}|{a['article_number_num']}{a.get('article_suffix', '')}",
                version_id, a["law_name"], a.get("law_type"), a.get("law_type_label"),
                a["article_number"], a["article_number_num"], a.get("article_suffix", ""),
                a.get("chapter"), a.get("section"), a["content"], a["content"],
                a["content_hash"], a.get("publish_date"), a.get("effective_date"),
                a.get("issuing_authority"), initial_status(a.get("effective_date")), a["source_file"],
            )
            for a in law["articles"]
        ],
    )
    return version_id


def supersede_old(conn: sqlite3.Connection, law: dict, new_version_id: int) -> int:
    """把同一部法的旧版本软删除：保留条文（供旧法适用），标 superseded。

    只按 norm_name 在本库现查，不用 law["old_version_id"]：
    那个 id 是「打包这份计划的那台机器」上的行号，到了用户本地库可能已经是
    另一部完全无关的法规。照它去 UPDATE，会把无关法规整部作废且没有任何报错。
    增量包本来就是跨库传递的，任何自增主键都不该跟着走。

    ★ 新版还没到施行日（pending）时一律不动旧版：此刻旧版仍是现行有效，
    提前降级会让这部法在当前库里「一条可用正文都不剩」，检索只能拿到明年才
    施行的文本。真正的降级由 ingest.schema.refresh_pending_status() 在施行日
    当天完成。
    """
    if initial_status(law.get("effective_date")) == STATUS_PENDING:
        return 0

    norm = law.get("norm_name") or norm_law_name(law["law_name"])
    pub = law.get("publish_date") or ""
    old_ids = [
        r[0] for r in conn.execute(
            "SELECT id FROM law_versions "
            "WHERE norm_name=? AND status='effective' AND id<>? "
            "  AND COALESCE(publish_date,'') <= ?",
            (norm, new_version_id, pub),
        ).fetchall()
    ]
    total = 0
    for old_id in old_ids:
        conn.execute(
            "UPDATE law_versions SET status='superseded', superseded_by=?, superseded_at=? WHERE id=?",
            (new_version_id, now_iso(), old_id),
        )
        conn.execute(
            """UPDATE law_articles SET status='repealed', repeal_date=?, replaced_by=?, repeal_reason=?
               WHERE version_id=?""",
            (law.get("effective_date") or "", law["law_name"], "被新修订版本取代", old_id),
        )
        total += conn.execute(
            "SELECT COUNT(*) FROM law_articles WHERE version_id=?", (old_id,)).fetchone()[0]
    return total


def apply_plan(db_path: str, plan_path: str, approve: bool, vectors: bool, force: bool) -> dict:
    if not approve:
        raise SystemExit("未加 --approve，拒绝写库。请先看报告确认。")

    with open(plan_path, encoding="utf-8") as f:
        plan = json.load(f)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    current = meta_get(conn, "corpus_version", "0")
    if plan["base_corpus_version"] != current and not force:
        raise SystemExit(
            f"报告基于库版本 {plan['base_corpus_version']}，当前库已变为 {current}。"
            "请重新生成报告；确定要以旧报告覆盖请加 --force。"
        )

    batch_id = "upd-" + now_iso()
    conn.execute("INSERT INTO ingest_batches(id, started_at, note) VALUES (?,?,?)",
                 (batch_id, now_iso(), plan_path))
    added_laws = updated_laws = added_articles = changed_articles = removed_articles = 0
    touched: list[str] = []

    with conn:  # 单事务：任何一步失败全部回滚
        for law in plan["laws"]:
            # stale = 镜像里的文本比库里还旧，跳过，绝不回退版本
            if law["action"] in ("skip", "stale"):
                continue
            version_id = insert_new_version(conn, law, batch_id)
            if law["action"] == "new":
                added_laws += 1
            else:
                updated_laws += 1
                removed_articles += supersede_old(conn, law, version_id)
            added_articles += law["stats"]["added"]
            changed_articles += law["stats"]["changed"]
            touched.append(law["norm_name"])
            conn.execute(
                "INSERT OR REPLACE INTO ingest_files(sha256, filename, law_name, version_id, batch_id, processed_at) "
                "VALUES (?,?,?,?,?,?)",
                (law["sha256"], law["source_file"], law["law_name"], version_id, batch_id, now_iso()),
            )

        conn.execute(
            """UPDATE ingest_batches SET finished_at=?, file_count=?, added_laws=?, updated_laws=?,
               added_articles=?, changed_articles=?, removed_articles=? WHERE id=?""",
            (now_iso(), len(plan["laws"]), added_laws, updated_laws, added_articles,
             changed_articles, removed_articles, batch_id),
        )
        create_fts(conn)
        version = bump_corpus_version(conn)

        # data_cutoff / snapshot_date 原来只有 apply_delta 会写，本地入库这条路上
        # 完全没人写 —— 结果入库了 10 月的法规，页脚还显示「条文截至 9 月」。
        # 这两个字段是用户判断「这份数据新不新」的唯一依据，必须跟着入库走。
        cutoff = conn.execute(
            "SELECT MAX(publish_date) FROM law_versions "
            "WHERE publish_date IS NOT NULL AND publish_date != ''"
        ).fetchone()[0] or ""
        # 未来日期是脏数据（正文里的期限被误当公布日），不能拿它顶高截止日
        today = datetime.date.today().isoformat()
        if cutoff > today:
            cutoff = today
        meta_set(conn, "data_cutoff", cutoff)
        meta_set(conn, "snapshot_date", today)
        conn.commit()

    verify = verify_db(conn, touched)
    conn.close()

    vector_report = None
    if vectors:
        vector_report = rebuild_vectors(plan, touched)

    return {
        "batch_id": batch_id,
        "corpus_version": version,
        "added_laws": added_laws,
        "updated_laws": updated_laws,
        "added_articles": added_articles,
        "changed_articles": changed_articles,
        "removed_articles": removed_articles,
        "touched": len(touched),
        "verify": verify,
        "vectors": vector_report,
    }


def verify_db(conn: sqlite3.Connection, touched: list[str]) -> dict:
    """入库后自检：条数、重号、空正文。"""
    problems: list[str] = []
    for norm in touched:
        version = conn.execute(
            "SELECT * FROM law_versions WHERE norm_name=? AND status='effective' "
            "ORDER BY COALESCE(publish_date,'') DESC, id DESC LIMIT 1", (norm,)).fetchone()
        if version is None:
            # 全新法可能整部都还在待生效期（如 2027-01-01 才施行的法律），
            # 这时「没有生效版本」是正确状态而非缺陷——库里确实一条现行条文
            # 都不该有。拿待生效版接着做条数与空正文自检，只有连待生效版都
            # 找不到，才算这部法根本没入库。
            version = conn.execute(
                "SELECT * FROM law_versions WHERE norm_name=? AND status='pending' "
                "ORDER BY COALESCE(publish_date,'') DESC, id DESC LIMIT 1", (norm,)).fetchone()
        if version is None:
            problems.append(f"{norm}: 无生效版本")
            continue
        rows = conn.execute(
            "SELECT article_number_num, article_suffix, COUNT(*) c FROM law_articles "
            "WHERE version_id=? GROUP BY 1,2 HAVING c>1", (version["id"],)).fetchall()
        if rows:
            problems.append(f"{version['law_name']}: {len(rows)} 处条号重复")
        empty = conn.execute(
            "SELECT COUNT(*) FROM law_articles WHERE version_id=? AND TRIM(content)=''",
            (version["id"],)).fetchone()[0]
        if empty:
            problems.append(f"{version['law_name']}: {empty} 条正文为空")
        if version["article_count"] != conn.execute(
                "SELECT COUNT(*) FROM law_articles WHERE version_id=?", (version["id"],)).fetchone()[0]:
            problems.append(f"{version['law_name']}: 条数与版本记录不一致")
    return {"ok": not problems, "problems": problems}


def rebuild_vectors(plan: dict, touched: list[str]) -> dict:
    """只重建本次涉及法规的向量（其余不动）。向量库缺失或依赖未装时跳过。"""
    chroma_path = os.environ.get("LAW_CHROMA_PATH", "./data/legal_vector_db")
    if not os.path.isdir(chroma_path):
        return {"status": "skipped", "reason": f"向量库目录不存在: {chroma_path}"}
    try:
        import chromadb  # noqa: PLC0415
        from sentence_transformers import SentenceTransformer  # noqa: PLC0415
    except ImportError as exc:
        return {"status": "skipped", "reason": f"依赖缺失: {exc}"}

    model = SentenceTransformer(os.environ.get("LAW_EMBEDDING_MODEL", "BAAI/bge-large-zh-v1.5"))
    client = chromadb.PersistentClient(path=chroma_path)
    collection = client.get_collection(os.environ.get("LAW_COLLECTION_NAME", "chinese_laws"))
    rebuilt = 0
    for law in plan["laws"]:
        if law["norm_name"] not in touched:
            continue
        try:
            collection.delete(where={"law_name": law["law_name"]})
        except Exception:  # noqa: BLE001
            pass
        docs = [a["content"] for a in law["articles"]]
        metas = [
            {
                "law_name": a["law_name"], "law_type": a.get("law_type") or "",
                "law_type_label": a.get("law_type_label") or "",
                "article_number": a["article_number"],
                "article_number_num": a["article_number_num"],
                "article_suffix": a.get("article_suffix") or "",
                "chapter": a.get("chapter") or "", "section": a.get("section") or "",
                "content": a["content"], "publish_date": a.get("publish_date") or "",
                "effective_date": a.get("effective_date") or "",
                "status": "effective", "repeal_date": "", "replaced_by": "",
            }
            for a in law["articles"]
        ]
        ids = [f"{law['source_file']}|{a['article_number_num']}{a.get('article_suffix','')}"
               for a in law["articles"]]
        embeddings = model.encode(docs, normalize_embeddings=True, batch_size=32).tolist()
        collection.add(ids=ids, documents=docs, metadatas=metas, embeddings=embeddings)
        rebuilt += 1
    return {"status": "ok", "rebuilt_laws": rebuilt}


def run_report(inbox: str, db: str, report_dir: str | None = None) -> tuple[str, str]:
    global REPORT_DIR
    if report_dir:
        REPORT_DIR = report_dir
    os.makedirs(REPORT_DIR, exist_ok=True)
    plan = build_plan(db, inbox)
    stamp = plan["generated_at"].replace(":", "").replace("-", "")[:15]
    plan_path = os.path.join(REPORT_DIR, f"changes-{stamp}.plan.json")
    html_path = os.path.join(REPORT_DIR, f"changes-{stamp}.html")
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=1)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(plan))
    return plan_path, html_path


def main() -> None:
    ap = argparse.ArgumentParser(description="法规增量更新（先报告，后确认写库）")
    ap.add_argument("--inbox", default=DEFAULT_INBOX)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--apply", metavar="PLAN_JSON", help="应用某份 plan.json")
    ap.add_argument("--approve", action="store_true", help="确认写库（必须显式给出）")
    ap.add_argument("--vectors", action="store_true", help="顺带重建涉及法规的向量索引")
    ap.add_argument("--force", action="store_true", help="忽略库版本不一致")
    ap.add_argument("--no-convert", action="store_true", help="跳过 doc/docx 转换")
    ap.add_argument("--reports-dir", default=None, help=f"报告输出目录（默认 {REPORT_DIR}）")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit(f"数据库不存在：{args.db}（请先放入数据包或运行 build_sqlite_from_markdown）")

    if args.apply:
        info = apply_plan(args.db, args.apply, args.approve, args.vectors, args.force)
        print(f"已写入：批次 {info['batch_id']}，数据版本 {info['corpus_version']}")
        print(f"  新法规 {info['added_laws']} ｜ 修订 {info['updated_laws']} ｜ "
              f"新增条 {info['added_articles']} ｜ 修改条 {info['changed_articles']} ｜ "
              f"旧版置为失效 {info['removed_articles']} 条")
        print(f"  自检：{'通过' if info['verify']['ok'] else '发现问题'}")
        for p in info["verify"]["problems"]:
            print(f"    - {p}")
        if info["vectors"]:
            print(f"  向量索引：{info['vectors']}")
        return

    if not args.no_convert:
        converted = convert_inbox(args.inbox)
        if converted:
            print(f"已转换 {len(converted)} 个 Word 文件到 {args.inbox}/_converted/")
    plan_path, html_path = run_report(args.inbox, args.db, args.reports_dir)
    with open(plan_path, encoding="utf-8") as f:
        summary = json.load(f)["summary"]
    print(f"报告：{html_path}")
    print(f"计划：{plan_path}")
    print(f"  新法规 {summary['new_laws']} ｜ 修订 {summary['updated_laws']} ｜ 无变化 {summary['skipped_laws']} ｜ "
          f"新增条 {summary['added_articles']} ｜ 修改条 {summary['changed_articles']} ｜ 删除条 {summary['removed_articles']}")
    print("确认无误后执行：")
    print(f"  python tools/update.py --apply {plan_path} --approve")


if __name__ == "__main__":
    main()
