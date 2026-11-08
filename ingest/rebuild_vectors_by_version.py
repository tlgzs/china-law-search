# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""按版本增量重建 ChromaDB 向量（G7 条文重切后使用）。

与 `ingest_to_vectordb.py` 的区别：本脚本不从 Markdown 重新解析，而是直接读
重切后的 SQLite `law_articles`，保证向量正文与库内条文逐字一致。

流程（每个受影响的版本）：
  1. 按 metadata `source_file` 删除该版本的全部旧向量（含重切后已删除的条号）；
  2. 从 `law_articles` 读取该版本当前条文，逐批 embedding 后 upsert；
  3. 回读校验：该 source_file 的向量条数 = 库内该版本条文数。

前提：`law_versions.source_file` 在库内唯一（同一文件对应多个版本时本脚本拒绝执行）。

用法（在 fmzy 上跑，需 GPU / 本地模型缓存）：
  python ingest/rebuild_vectors_by_version.py \
      --db /home/fumin/law_ingest/law_data_20261107.db \
      --chroma /home/fumin/law_ingest/legal_vector_db \
      --plan reports/g7-round2-batches/batch1-plan-20261107.csv \
      --plan reports/g7-round2-batches/batch2-plan-20261107.csv \
      --plan reports/g7-round2-batches/batch3-plan-20261107.csv \
      --report reports/vectors-rebuild-20261107.json
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

from build_chromadb import (  # noqa: E402
    EMBEDDING_MODEL,
    build_metadata,
    build_text_for_embedding,
)

_ROW_SELECT = """SELECT vector_id, law_name, law_type, law_type_label,
                        article_number, article_number_num, chapter, section,
                        article, content, publish_date, effective_date,
                        issuing_authority, status, source_file,
                        repeal_date, repeal_reason, replaced_by, is_principle_clause
                 FROM law_articles WHERE version_id = ? ORDER BY id"""


def load_versions(plans: list[str], raw: str) -> list[int]:
    ids: set[int] = set()
    for plan in plans:
        with open(plan, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if (row.get("version_id") or "").strip():
                    ids.add(int(row["version_id"]))
    for token in (raw or "").split(","):
        if token.strip():
            ids.add(int(token))
    return sorted(ids)


def check_source_files(conn: sqlite3.Connection, version_ids: list[int]) -> list[str]:
    marks = ",".join("?" for _ in version_ids)
    rows = conn.execute(
        f"SELECT id, source_file FROM law_versions WHERE id IN ({marks})",
        version_ids).fetchall()
    missing = [vid for vid in version_ids if vid not in {r["id"] for r in rows}]
    if missing:
        raise SystemExit(f"版本不存在: {missing}")
    names = [r["source_file"] or "" for r in rows]
    if any(not n for n in names):
        raise SystemExit("存在 source_file 为空的版本，无法定位向量")
    shared = conn.execute(
        f"SELECT source_file, COUNT(*) FROM law_versions "
        f"WHERE source_file IN ({','.join('?' for _ in names)}) "
        f"GROUP BY source_file HAVING COUNT(*) > 1", names).fetchall()
    if shared:
        raise SystemExit(
            "source_file 被多个版本共用，不能按文件删向量: "
            + ", ".join(r[0] for r in shared))
    return names


def load_vector_ids_by_source(chroma_path: str, wanted: set[str]) -> dict[str, list[str]]:
    """从 ChromaDB 底层 SQLite 一次性读出 vector_id 并按 source_file 分组。

    不做 `embeddings JOIN embedding_metadata`：2026-11-08 实测该库
    `embedding_metadata` 存在页级损坏（quick_check 报 Tree 25 page 205531
    cell 越界），JOIN 会直接抛 `database disk image is malformed`。
    `vector_id` 本身是 `{source_file}|{article_number_num}`，直接从 `embeddings`
    读 id 再按最后一个 `|` 切分即可，既不碰损坏页，也比 JOIN 快。

    仍不走 `collection.get()`：ChromaDB 不按 metadata 建索引，get/delete 的
    where 过滤是逐条扫描，75 万条量级下单个版本要 18 秒以上。
    """
    db = os.path.join(chroma_path, "chroma.sqlite3")
    if not os.path.exists(db):
        raise SystemExit(f"找不到 ChromaDB 元数据库: {db}")
    chroma_conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = chroma_conn.execute("SELECT embedding_id FROM embeddings").fetchall()
    finally:
        chroma_conn.close()
    grouped: dict[str, list[str]] = defaultdict(list)
    for (vector_id,) in rows:
        if not vector_id or "|" not in vector_id:
            continue
        source_file, _ = vector_id.rsplit("|", 1)
        if source_file in wanted:
            grouped[source_file].append(vector_id)
    return grouped


def main() -> None:
    ap = argparse.ArgumentParser(description="按版本增量重建 ChromaDB 向量")
    ap.add_argument("--db", default="./data/law_data.db")
    ap.add_argument("--chroma", default="./data/chroma_db")
    ap.add_argument("--collection", default="chinese_laws")
    ap.add_argument("--plan", action="append", default=[], help="可重复，读 version_id 列")
    ap.add_argument("--versions", default="", help="逗号分隔的 version_id")
    ap.add_argument("--batch-size", type=int, default=200)
    ap.add_argument("--encode-batch", type=int, default=16,
                    help="GPU 编码批大小；fmzy 显存只剩约 3.4 GB，64 会 OOM 后段错误")
    ap.add_argument("--cpu", action="store_true", help="强制 CPU（仅测试用）")
    ap.add_argument("--report", default="", help="输出 JSON 报告路径")
    args = ap.parse_args()

    version_ids = load_versions(args.plan, args.versions)
    if not version_ids:
        raise SystemExit("未提供任何 version_id（--plan 或 --versions）")

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    source_files = check_source_files(conn, version_ids)
    expected_rows = sum(
        conn.execute("SELECT COUNT(*) FROM law_articles WHERE version_id = ?",
                     (vid,)).fetchone()[0] for vid in version_ids)

    print("=" * 60)
    print("按版本增量重建向量")
    print(f"  SQLite    : {args.db}")
    print(f"  ChromaDB  : {args.chroma}")
    print(f"  Collection: {args.collection}")
    print(f"  版本数    : {len(version_ids)}")
    print(f"  条文数    : {expected_rows}")
    print("=" * 60)

    import chromadb
    import torch
    from sentence_transformers import SentenceTransformer

    device = "cpu" if args.cpu or not torch.cuda.is_available() else "cuda"
    print(f"设备: {device}")
    model = SentenceTransformer(EMBEDDING_MODEL, device=device)

    client = chromadb.PersistentClient(path=args.chroma)
    collection = client.get_or_create_collection(
        name=args.collection, metadata={"hnsw:space": "cosine"})
    before_count = collection.count()
    print(f"ChromaDB 现有记录: {before_count} 条")

    # ChromaDB 没有 metadata 索引：get(where=...) / delete(where=...) 都是全量扫描。
    # 从底层 SQLite 一次性取回旧 vector_id 后，后续全部走主键 delete。
    print("预取旧 vector_id 并按 source_file 分组...")
    t_pre = time.time()
    old_ids_by_file = load_vector_ids_by_source(args.chroma, set(source_files))
    print(f"  预取完成 {time.time() - t_pre:.1f}s，命中 {len(old_ids_by_file)} 个文件、"
          f"{sum(len(v) for v in old_ids_by_file.values())} 条旧向量")
    print("  提示：同一条号重切后仍存在时 vector_id 不变，delete + upsert 等价于覆盖；"
          "重切后已消失的条号会被 delete 清掉，不留脏向量。")

    # 阶段 1：一次性删掉这 9370 个文件的全部旧向量。
    # 实测逐版本 delete+upsert 只有 0.4 版本/秒（每版本两次 ChromaDB 事务开销约 2 秒），
    # 9370 个版本要 6.5 小时；改成先整体删、再按条文数批量 upsert 可压到约 1 小时。
    all_old_ids = [i for ids in old_ids_by_file.values() for i in ids]
    print(f"阶段 1/2：删除 {len(all_old_ids)} 条旧向量（按主键分批）...")
    t_del = time.time()
    for start in range(0, len(all_old_ids), 2000):
        collection.delete(ids=all_old_ids[start:start + 2000])
    print(f"  删除完成 {time.time() - t_del:.1f}s")

    # 阶段 2：按批次聚合条文再 upsert，把 ChromaDB 事务次数从「每版本 2 次」降到
    # 「每 500 条 1 次」。同一条号重切后仍存在时 vector_id 不变，upsert 等价于覆盖。
    print("阶段 2/2：逐版本读条文，按批 embedding + upsert ...")
    t0 = time.time()
    done: list[dict] = []
    buf_ids: list[str] = []
    buf_texts: list[str] = []
    buf_metas: list[dict] = []

    def encode_chunk(texts: list[str]) -> list[list[float]]:
        """编码一批文本；显存不足时清缓存并减半重试，避免 NVML 异常路径直接段错误。"""
        batch = args.encode_batch
        while True:
            try:
                vectors = model.encode(
                    texts, batch_size=batch, show_progress_bar=False,
                    convert_to_numpy=True, normalize_embeddings=True)
                return vectors.tolist()
            except RuntimeError as exc:
                if batch <= 1 or "out of memory" not in str(exc).lower():
                    raise
                batch //= 2
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
                print(f"  显存不足，编码批降为 {batch} 重试", flush=True)

    def flush_buffer() -> None:
        while buf_ids:
            take = min(args.batch_size, len(buf_ids))
            chunk = slice(0, take)
            embeddings = encode_chunk(buf_texts[chunk])
            collection.upsert(
                ids=buf_ids[chunk], embeddings=embeddings,
                documents=buf_texts[chunk], metadatas=buf_metas[chunk])
            del buf_ids[chunk], buf_texts[chunk], buf_metas[chunk]

    for idx, (vid, fname) in enumerate(zip(version_ids, source_files), 1):
        rows = conn.execute(_ROW_SELECT, (vid,)).fetchall()
        buf_ids.extend(r[0] for r in rows)
        buf_texts.extend(build_text_for_embedding(r) for r in rows)
        buf_metas.extend(build_metadata(r) for r in rows)
        done.append({"version_id": vid, "source_file": fname, "vectors": len(rows)})
        if len(buf_ids) >= args.batch_size:
            flush_buffer()
        if idx % 500 == 0 or idx == len(version_ids):
            rate = idx / (time.time() - t0)
            print(f"  进度: {idx}/{len(version_ids)} 版本  {rate:.1f} 版本/秒", flush=True)
    flush_buffer()

    print("回读校验：按 source_file 统计向量数...")
    verify = load_vector_ids_by_source(args.chroma, set(source_files))
    got_total = sum(len(v) for v in verify.values())
    missing_files = sorted(set(source_files) - set(verify))
    if missing_files:
        raise SystemExit(f"回读校验失败: {len(missing_files)} 个文件无向量，例: {missing_files[:3]}")
    if got_total != expected_rows:
        raise SystemExit(
            f"回读校验失败: 向量 {got_total} != 条文 {expected_rows}")
    print(f"  校验通过: {len(verify)} 个文件 / {got_total} 条向量 = 条文数")

    after_count = collection.count()
    report = {
        "versions": len(version_ids),
        "articles_expected": expected_rows,
        "vectors_written": sum(d["vectors"] for d in done),
        "vectors_verified": got_total,
        "chroma_before": before_count,
        "chroma_after": after_count,
        "elapsed_sec": round(time.time() - t0, 1),
        "details": done,
    }
    print(json.dumps({k: v for k, v in report.items() if k != "details"},
                     ensure_ascii=False, indent=2))
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告: {args.report}")
    conn.close()


if __name__ == "__main__":
    main()
