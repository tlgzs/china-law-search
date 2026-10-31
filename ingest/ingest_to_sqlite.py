# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""
法规原文 SQLite 构建脚本。

从已导入、已去重的 ChromaDB 元数据导出法条原文，生成独立的 law_data.db。
该库用于法规名、条号、关键词等精确检索；ChromaDB 继续负责语义向量检索。
"""

import argparse
import sqlite3
import time
from pathlib import Path
from typing import Any


DEFAULT_CHROMA_DB_PATH = "./data/legal_vector_db"
DEFAULT_OUTPUT_PATH = "./data/law_data.db"
DEFAULT_COLLECTION_NAME = "chinese_laws"


def create_schema(conn: sqlite3.Connection) -> None:
    """创建法条原文表和检索索引。"""
    conn.executescript(
        """
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous = NORMAL;

        CREATE TABLE law_articles (
            id INTEGER PRIMARY KEY,
            vector_id TEXT NOT NULL UNIQUE,
            law_name TEXT NOT NULL,
            law_type TEXT,
            law_type_label TEXT,
            article_number TEXT,
            article_number_num INTEGER,
            chapter TEXT,
            section TEXT,
            article TEXT NOT NULL,
            content TEXT NOT NULL,
            publish_date TEXT,
            effective_date TEXT,
            issuing_authority TEXT,
            status TEXT,
            source_file TEXT NOT NULL
        );

        CREATE INDEX idx_law_articles_law_name ON law_articles(law_name);
        CREATE INDEX idx_law_articles_law_name_article_num ON law_articles(law_name, article_number_num);
        CREATE INDEX idx_law_articles_article_number_num ON law_articles(article_number_num);
        CREATE INDEX idx_law_articles_source_file ON law_articles(source_file);
        CREATE INDEX idx_law_articles_law_type ON law_articles(law_type);
        CREATE INDEX idx_law_articles_publish_date ON law_articles(publish_date);
        """
    )


def create_fts_index(conn: sqlite3.Connection) -> bool:
    """创建中文 trigram 全文索引；SQLite 不支持时返回 False。"""
    try:
        conn.execute(
            """
            CREATE VIRTUAL TABLE law_articles_fts USING fts5(
                law_name,
                article,
                content,
                chapter,
                section,
                content='law_articles',
                content_rowid='id',
                tokenize='trigram'
            )
            """
        )
        conn.execute(
            """
            INSERT INTO law_articles_fts(rowid, law_name, article, content, chapter, section)
            SELECT id, law_name, article, content, chapter, section
            FROM law_articles
            """
        )
        return True
    except sqlite3.Error as exc:
        print(f"FTS trigram index skipped: {exc}", flush=True)
        return False


def get_metadata_segment_id(conn: sqlite3.Connection, collection_name: str) -> str:
    row = conn.execute(
        """
        SELECT s.id
        FROM chroma.segments AS s
        JOIN chroma.collections AS c ON c.id = s.collection
        WHERE c.name = ? AND s.scope = ?
        LIMIT 1
        """,
        (collection_name, "METADATA"),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Metadata segment not found for collection: {collection_name}")
    return str(row[0])


def insert_from_chroma(
    conn: sqlite3.Connection,
    *,
    chroma_sqlite_path: Path,
    collection_name: str,
) -> int:
    conn.execute("ATTACH DATABASE ? AS chroma", (str(chroma_sqlite_path),))
    metadata_segment_id = get_metadata_segment_id(conn, collection_name)
    cursor = conn.execute(
        """
            INSERT INTO law_articles (
                id,
                vector_id,
                law_name,
                law_type,
                law_type_label,
                article_number,
                article_number_num,
                chapter,
                section,
                article,
                content,
                publish_date,
                effective_date,
                issuing_authority,
                status,
                source_file
            )
            SELECT
                e.id AS id,
                e.embedding_id AS vector_id,
                COALESCE(law_name.string_value, '') AS law_name,
                COALESCE(law_type.string_value, '') AS law_type,
                COALESCE(law_type_label.string_value, '') AS law_type_label,
                COALESCE(article_number.string_value, '') AS article_number,
                article_number_num.int_value AS article_number_num,
                COALESCE(chapter.string_value, '') AS chapter,
                COALESCE(section.string_value, '') AS section,
                COALESCE(article.string_value, content.string_value, doc.string_value, '') AS article,
                COALESCE(content.string_value, article.string_value, doc.string_value, '') AS content,
                COALESCE(publish_date.string_value, '') AS publish_date,
                COALESCE(effective_date.string_value, '') AS effective_date,
                COALESCE(issuing_authority.string_value, '') AS issuing_authority,
                COALESCE(status.string_value, '') AS status,
                COALESCE(source_file.string_value, '') AS source_file
            FROM chroma.embeddings AS e
            LEFT JOIN chroma.embedding_metadata AS law_name
                ON law_name.id = e.id AND law_name.key = 'law_name'
            LEFT JOIN chroma.embedding_metadata AS law_type
                ON law_type.id = e.id AND law_type.key = 'law_type'
            LEFT JOIN chroma.embedding_metadata AS law_type_label
                ON law_type_label.id = e.id AND law_type_label.key = 'law_type_label'
            LEFT JOIN chroma.embedding_metadata AS article_number
                ON article_number.id = e.id AND article_number.key = 'article_number'
            LEFT JOIN chroma.embedding_metadata AS article_number_num
                ON article_number_num.id = e.id AND article_number_num.key = 'article_number_num'
            LEFT JOIN chroma.embedding_metadata AS chapter
                ON chapter.id = e.id AND chapter.key = 'chapter'
            LEFT JOIN chroma.embedding_metadata AS section
                ON section.id = e.id AND section.key = 'section'
            LEFT JOIN chroma.embedding_metadata AS article
                ON article.id = e.id AND article.key = 'article'
            LEFT JOIN chroma.embedding_metadata AS content
                ON content.id = e.id AND content.key = 'content'
            LEFT JOIN chroma.embedding_metadata AS publish_date
                ON publish_date.id = e.id AND publish_date.key = 'publish_date'
            LEFT JOIN chroma.embedding_metadata AS effective_date
                ON effective_date.id = e.id AND effective_date.key = 'effective_date'
            LEFT JOIN chroma.embedding_metadata AS issuing_authority
                ON issuing_authority.id = e.id AND issuing_authority.key = 'issuing_authority'
            LEFT JOIN chroma.embedding_metadata AS status
                ON status.id = e.id AND status.key = 'status'
            LEFT JOIN chroma.embedding_metadata AS source_file
                ON source_file.id = e.id AND source_file.key = 'source_file'
            LEFT JOIN chroma.embedding_metadata AS doc
                ON doc.id = e.id AND doc.key = 'chroma:document'
            WHERE e.segment_id = ?
              AND COALESCE(law_name.string_value, '') != ''
              AND COALESCE(source_file.string_value, '') != ''
              AND COALESCE(article.string_value, content.string_value, doc.string_value, '') != ''
        """,
        (metadata_segment_id,),
    )
    return cursor.rowcount


def build_law_data_db(
    *,
    chroma_db_path: Path,
    output_path: Path,
    collection_name: str,
    batch_size: int = 10000,
    replace: bool = True,
    create_fts: bool = True,
) -> dict[str, Any]:
    del batch_size
    chroma_sqlite_path = chroma_db_path / "chroma.sqlite3"
    if not chroma_sqlite_path.exists():
        raise FileNotFoundError(f"Chroma SQLite not found: {chroma_sqlite_path}")
    if output_path.exists() and not replace:
        raise FileExistsError(f"Output already exists: {output_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_suffix(output_path.suffix + ".tmp")
    if temp_path.exists():
        temp_path.unlink()

    started = time.time()
    conn = sqlite3.connect(temp_path)
    try:
        create_schema(conn)
        inserted = insert_from_chroma(
            conn,
            chroma_sqlite_path=chroma_sqlite_path,
            collection_name=collection_name,
        )
        fts_created = create_fts_index(conn) if create_fts else False
        conn.commit()
    finally:
        conn.close()

    temp_path.replace(output_path)
    return {
        "inserted": inserted,
        "fts_created": fts_created,
        "elapsed": time.time() - started,
        "output_path": str(output_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build law_data.db from ChromaDB metadata.")
    parser.add_argument("--chroma-db-path", default=DEFAULT_CHROMA_DB_PATH)
    parser.add_argument("--output-path", default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--collection-name", default=DEFAULT_COLLECTION_NAME)
    parser.add_argument("--no-replace", action="store_true", help="Do not replace an existing output DB.")
    parser.add_argument("--no-fts", action="store_true", help="Skip FTS5 trigram index creation.")
    parser.add_argument("--batch-size", type=int, default=10000)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    stats = build_law_data_db(
        chroma_db_path=Path(args.chroma_db_path),
        output_path=Path(args.output_path),
        collection_name=args.collection_name,
        batch_size=args.batch_size,
        replace=not args.no_replace,
        create_fts=not args.no_fts,
    )
    print("law_data.db build completed.", flush=True)
    print(f"Inserted records: {stats['inserted']}", flush=True)
    print(f"FTS created: {stats['fts_created']}", flush=True)
    print(f"Output path: {stats['output_path']}", flush=True)
    print(f"Elapsed seconds: {stats['elapsed']:.1f}", flush=True)


if __name__ == "__main__":
    main()
