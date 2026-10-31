# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟

"""从 SQLite 数据库构建 ChromaDB 向量库。

直接从 law_articles 表读取法条，用 sentence-transformers 生成 embedding，
写入 ChromaDB collection。支持断点续传。

用法:
    # 在 fmzy 上用 venv 运行
    cd <本项目根目录>
    python ingest/build_chromadb.py

    # 断点续传
    python ingest/build_chromadb.py --resume
"""

import argparse
import os
import sqlite3
import sys
import time

# 离线模式：fmzy 服务器无外网，模型已缓存在本地
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

# ============================================================
# 配置
# ============================================================

SQLITE_PATH = "./data/law_data.db"
CHROMA_PATH = "./data/chroma_db"
COLLECTION_NAME = "chinese_laws"
EMBEDDING_MODEL = "BAAI/bge-large-zh-v1.5"
BATCH_SIZE = 500  # 每批 embedding + 写入的条数


def get_sqlite_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM law_articles").fetchone()[0]


def get_sqlite_batch(conn: sqlite3.Connection, offset: int, limit: int):
    """从 SQLite 读取一批法条。"""
    rows = conn.execute(
        """SELECT vector_id, law_name, law_type, law_type_label,
                  article_number, article_number_num, chapter, section,
                  article, content, publish_date, effective_date,
                  issuing_authority, status, source_file,
                  repeal_date, repeal_reason, replaced_by, is_principle_clause
           FROM law_articles
           ORDER BY id
           LIMIT ? OFFSET ?""",
        (limit, offset),
    ).fetchall()
    return rows


def build_text_for_embedding(row) -> str:
    """构建用于 embedding 的文本（法条正文 + 上下文）。"""
    parts = []
    content = row[9] or ""  # content
    if content:
        parts.append(content)
    chapter = row[6] or ""  # chapter
    if chapter:
        parts.append(f"（{chapter}）")
    law_name = row[1] or ""  # law_name
    if law_name:
        parts.append(f"《{law_name}》")
    return "\n".join(parts)


def build_metadata(row) -> dict:
    """构建 ChromaDB metadata。"""
    return {
        "law_name": row[1] or "",
        "law_type": row[2] or "",
        "law_type_label": row[3] or "",
        "article_number": row[4] or "",
        "article_number_num": row[5] or 0,
        "chapter": row[6] or "",
        "section": row[7] or "",
        "content": row[9] or "",
        "publish_date": row[10] or "",
        "effective_date": row[11] or "",
        "issuing_authority": row[12] or "",
        "status": row[13] or "effective",
        "source_file": row[14] or "",
        "repeal_date": row[15] or "",
        "repeal_reason": row[16] or "",
        "replaced_by": row[17] or "",
        "is_principle_clause": row[18] or 0,
    }


def main():
    parser = argparse.ArgumentParser(description="从 SQLite 构建 ChromaDB 向量库")
    parser.add_argument("--resume", action="store_true", help="断点续传")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="批次大小")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少条（0=全部）")
    args = parser.parse_args()

    print("=" * 60)
    print("ChromaDB 向量库构建")
    print("=" * 60)
    print(f"SQLite: {SQLITE_PATH}")
    print(f"ChromaDB: {CHROMA_PATH}")
    print(f"Collection: {COLLECTION_NAME}")
    print(f"Model: {EMBEDDING_MODEL}")
    print(f"Batch size: {args.batch_size}")
    print()

    # ============================================================
    # 1. 初始化 embedding 模型
    # ============================================================
    print("加载 embedding 模型...")
    from sentence_transformers import SentenceTransformer
    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"设备: {device}")
    if device == "cuda":
        print(f"GPU: {torch.cuda.get_device_name(0)}")

    model = SentenceTransformer(EMBEDDING_MODEL, device=device)
    print(f"模型加载完成，embedding 维度: {model.get_sentence_embedding_dimension()}")
    print()

    # ============================================================
    # 2. 初始化 ChromaDB
    # ============================================================
    print("初始化 ChromaDB...")
    import chromadb

    client = chromadb.PersistentClient(path=CHROMA_PATH)

    # 获取或创建 collection
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    existing_count = collection.count()
    print(f"ChromaDB 现有记录: {existing_count} 条")
    print()

    # ============================================================
    # 3. 从 SQLite 读取并构建向量
    # ============================================================
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    total = get_sqlite_count(conn)
    print(f"SQLite 总记录: {total} 条")

    # 断点续传：跳过已导入的记录
    start_offset = existing_count if args.resume else 0
    if args.limit > 0:
        total_to_process = min(args.limit, total - start_offset)
    else:
        total_to_process = total - start_offset

    print(f"本次处理: {total_to_process} 条 (起始偏移: {start_offset})")
    print()

    batch_size = args.batch_size
    processed = 0
    errors = 0
    start_time = time.time()

    while processed < total_to_process:
        offset = start_offset + processed
        current_batch = min(batch_size, total_to_process - processed)

        try:
            rows = get_sqlite_batch(conn, offset, current_batch)
            if not rows:
                print(f"  SQLite 返回空结果，offset={offset}")
                break

            # 构建 embedding 文本
            texts = [build_text_for_embedding(r) for r in rows]
            ids = [r[0] for r in rows]  # vector_id
            metadatas = [build_metadata(r) for r in rows]

            # 生成 embeddings
            embeddings = model.encode(
                texts,
                batch_size=64,
                show_progress_bar=False,
                convert_to_numpy=True,
                normalize_embeddings=True,
            )

            # 写入 ChromaDB
            collection.upsert(
                ids=ids,
                embeddings=embeddings.tolist(),
                documents=texts,
                metadatas=metadatas,
            )

            processed += len(rows)

        except Exception as e:
            print(f"  错误 @ offset={offset}: {e}")
            errors += 1
            processed += current_batch  # 跳过这批
            continue

        # 进度显示
        elapsed = time.time() - start_time
        rate = processed / elapsed if elapsed > 0 else 0
        eta = (total_to_process - processed) / rate if rate > 0 else 0
        pct = processed / total_to_process * 100
        print(
            f"  进度: {processed}/{total_to_process} ({pct:.1f}%) "
            f"| 速度: {rate:.0f} 条/秒 "
            f"| 剩余: {eta:.0f}秒 "
            f"| ChromaDB: {collection.count()} 条",
            flush=True,
        )

    elapsed = time.time() - start_time
    print()
    print("=" * 60)
    print("构建完成!")
    print(f"  处理条数: {processed}")
    print(f"  错误次数: {errors}")
    print(f"  耗时: {elapsed:.1f}秒")
    print(f"  平均速度: {processed/elapsed:.0f} 条/秒" if elapsed > 0 else "")
    print(f"  ChromaDB 总记录: {collection.count()} 条")
    print()

    # 验证检索
    print("验证检索...")
    test_query = "民间借贷利率上限"
    query_embedding = model.encode([test_query], normalize_embeddings=True)
    results = collection.query(
        query_embeddings=query_embedding.tolist(),
        n_results=3,
    )
    for i, (doc, meta, dist) in enumerate(zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    )):
        print(f"  [{i+1}] {meta['law_name']} {meta['article_number']} (距离: {dist:.4f})")
        print(f"      {doc[:80]}...")

    conn.close()
    print()
    print("完成!")


if __name__ == "__main__":
    main()
