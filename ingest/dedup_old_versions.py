# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟

"""
法规向量库去重脚本。

删除同一法规名称下公布日期较早的文件版本，只保留公布日期最新的文件版本。
默认只做 dry-run；必须传入 --execute 才会实际删除 ChromaDB 和原文 SQLite 记录。
"""

import argparse
import csv
import re
import sqlite3
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import chromadb


DEFAULT_DB_PATH = "./data/legal_vector_db"
DEFAULT_SQLITE_PATH = "./data/law_data.db"
DEFAULT_COLLECTION_NAME = "chinese_laws"
VALID_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class LawFileVersion:
    law_name: str
    source_file: str
    publish_date: str
    article_count: int

    @property
    def sortable_publish_date(self) -> str:
        if VALID_DATE_PATTERN.match(self.publish_date or ""):
            return self.publish_date
        return "0000-00-00"


@dataclass(frozen=True)
class DedupPlan:
    total_records: int
    total_files: int
    multi_version_laws: int
    old_source_files: set[str]
    old_record_count: int
    kept_files: set[str]


def connect_chroma_sqlite(db_path: Path) -> sqlite3.Connection:
    sqlite_path = db_path / "chroma.sqlite3"
    if not sqlite_path.exists():
        raise FileNotFoundError(f"Chroma SQLite not found: {sqlite_path}")
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row
    return conn


def get_metadata_segment_id(conn: sqlite3.Connection, collection_name: str) -> str:
    row = conn.execute(
        """
        SELECT s.id
        FROM segments AS s
        JOIN collections AS c ON c.id = s.collection
        WHERE c.name = ? AND s.scope = ?
        LIMIT 1
        """,
        (collection_name, "METADATA"),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Metadata segment not found for collection: {collection_name}")
    return str(row["id"])


def load_file_versions(conn: sqlite3.Connection, metadata_segment_id: str) -> list[LawFileVersion]:
    rows = conn.execute(
        """
        SELECT
            COALESCE(law_name.string_value, '') AS law_name,
            COALESCE(source_file.string_value, '') AS source_file,
            COALESCE(MAX(publish_date.string_value), '') AS publish_date,
            COUNT(*) AS article_count
        FROM embeddings AS e
        JOIN embedding_metadata AS source_file
            ON source_file.id = e.id AND source_file.key = 'source_file'
        LEFT JOIN embedding_metadata AS law_name
            ON law_name.id = e.id AND law_name.key = 'law_name'
        LEFT JOIN embedding_metadata AS publish_date
            ON publish_date.id = e.id AND publish_date.key = 'publish_date'
        WHERE e.segment_id = ?
          AND COALESCE(law_name.string_value, '') != ''
          AND COALESCE(source_file.string_value, '') != ''
        GROUP BY law_name.string_value, source_file.string_value
        ORDER BY law_name.string_value, publish_date DESC, source_file.string_value
        """,
        (metadata_segment_id,),
    ).fetchall()
    return [
        LawFileVersion(
            law_name=str(row["law_name"]),
            source_file=str(row["source_file"]),
            publish_date=str(row["publish_date"]),
            article_count=int(row["article_count"]),
        )
        for row in rows
    ]


def build_dedup_plan(
    file_versions: list[LawFileVersion],
    *,
    keep_all_latest_date_ties: bool,
) -> DedupPlan:
    grouped: dict[str, list[LawFileVersion]] = defaultdict(list)
    for version in file_versions:
        grouped[version.law_name].append(version)

    old_source_files: set[str] = set()
    kept_files: set[str] = set()
    multi_version_laws = 0

    for versions in grouped.values():
        if len(versions) == 1:
            kept_files.add(versions[0].source_file)
            continue

        multi_version_laws += 1
        sorted_versions = sorted(
            versions,
            key=lambda item: (item.sortable_publish_date, item.source_file),
            reverse=True,
        )
        latest_date = sorted_versions[0].sortable_publish_date
        for version in sorted_versions:
            should_keep = (
                version.sortable_publish_date == latest_date
                if keep_all_latest_date_ties
                else version == sorted_versions[0]
            )
            if should_keep:
                kept_files.add(version.source_file)
            else:
                old_source_files.add(version.source_file)

    old_record_count = sum(
        version.article_count
        for version in file_versions
        if version.source_file in old_source_files
    )
    total_records = sum(version.article_count for version in file_versions)
    return DedupPlan(
        total_records=total_records,
        total_files=len(file_versions),
        multi_version_laws=multi_version_laws,
        old_source_files=old_source_files,
        old_record_count=old_record_count,
        kept_files=kept_files,
    )


def batched(items: list[str], batch_size: int) -> Iterable[list[str]]:
    for index in range(0, len(items), batch_size):
        yield items[index:index + batch_size]


def load_embedding_ids_for_sources(
    conn: sqlite3.Connection,
    metadata_segment_id: str,
    source_files: set[str],
    *,
    batch_size: int,
) -> list[str]:
    ids: list[str] = []
    sorted_sources = sorted(source_files)
    for batch in batched(sorted_sources, batch_size):
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""
            SELECT e.embedding_id
            FROM embeddings AS e
            JOIN embedding_metadata AS source_file
                ON source_file.id = e.id AND source_file.key = 'source_file'
            WHERE e.segment_id = ?
              AND source_file.string_value IN ({placeholders})
            """,
            [metadata_segment_id, *batch],
        ).fetchall()
        ids.extend(str(row["embedding_id"]) for row in rows)
    return ids


def delete_from_chroma(
    db_path: Path,
    collection_name: str,
    ids: list[str],
    *,
    batch_size: int,
) -> None:
    client = chromadb.PersistentClient(path=str(db_path))
    collection = client.get_collection(collection_name)
    total = len(ids)
    for index, batch in enumerate(batched(ids, batch_size), start=1):
        collection.delete(ids=batch)
        deleted = min(index * batch_size, total)
        print(f"Chroma delete progress: {deleted}/{total}", flush=True)


def delete_from_law_sqlite(sqlite_path: Path, source_files: set[str], *, batch_size: int) -> int:
    if not sqlite_path.exists():
        print(f"Law SQLite not found, skipped: {sqlite_path}", flush=True)
        return 0

    conn = sqlite3.connect(sqlite_path)
    try:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?",
            ("table", "law_articles"),
        ).fetchone()
        if table_exists is None:
            print("Table law_articles not found, SQLite delete skipped.", flush=True)
            return 0

        deleted = 0
        sorted_sources = sorted(source_files)
        for batch in batched(sorted_sources, batch_size):
            placeholders = ",".join("?" for _ in batch)
            cursor = conn.execute(
                f"DELETE FROM law_articles WHERE source_file IN ({placeholders})",
                batch,
            )
            deleted += cursor.rowcount
        conn.commit()
        return deleted
    finally:
        conn.close()


def write_plan_csv(path: Path, file_versions: list[LawFileVersion], plan: DedupPlan) -> None:
    with path.open("w", encoding="utf-8", newline="") as file_obj:
        writer = csv.writer(file_obj)
        writer.writerow(["action", "law_name", "source_file", "publish_date", "article_count"])
        for version in file_versions:
            action = "delete" if version.source_file in plan.old_source_files else "keep"
            writer.writerow([
                action,
                version.law_name,
                version.source_file,
                version.publish_date,
                version.article_count,
            ])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deduplicate old law versions in ChromaDB.")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--sqlite-path", default=DEFAULT_SQLITE_PATH)
    parser.add_argument("--collection-name", default=DEFAULT_COLLECTION_NAME)
    parser.add_argument("--execute", action="store_true", help="Actually delete old versions.")
    parser.add_argument(
        "--keep-single-latest",
        action="store_true",
        help="Keep only one latest file when multiple files share the latest publish_date.",
    )
    parser.add_argument("--delete-batch-size", type=int, default=5000)
    parser.add_argument("--source-batch-size", type=int, default=900)
    parser.add_argument("--plan-csv", default="dedup_old_versions_plan.csv")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db_path = Path(args.db_path)
    sqlite_path = Path(args.sqlite_path)
    started = time.time()

    conn = connect_chroma_sqlite(db_path)
    try:
        metadata_segment_id = get_metadata_segment_id(conn, args.collection_name)
        file_versions = load_file_versions(conn, metadata_segment_id)
        plan = build_dedup_plan(
            file_versions,
            keep_all_latest_date_ties=not args.keep_single_latest,
        )
        old_ids = load_embedding_ids_for_sources(
            conn,
            metadata_segment_id,
            plan.old_source_files,
            batch_size=args.source_batch_size,
        )
    finally:
        conn.close()

    plan_csv = Path(args.plan_csv)
    write_plan_csv(plan_csv, file_versions, plan)

    print(f"Mode: {'EXECUTE' if args.execute else 'DRY_RUN'}", flush=True)
    print(f"Collection: {args.collection_name}", flush=True)
    print(f"Total article records: {plan.total_records}", flush=True)
    print(f"Total source files: {plan.total_files}", flush=True)
    print(f"Multi-version laws: {plan.multi_version_laws}", flush=True)
    print(f"Old source files: {len(plan.old_source_files)}", flush=True)
    print(f"Old article records by file count: {plan.old_record_count}", flush=True)
    print(f"Old Chroma ids: {len(old_ids)}", flush=True)
    print(f"Kept source files: {len(plan.kept_files)}", flush=True)
    print(f"Plan CSV: {plan_csv}", flush=True)

    if not args.execute:
        print("Dry run only. Pass --execute to delete old versions.", flush=True)
        return

    if len(old_ids) != plan.old_record_count:
        raise RuntimeError(
            f"Old id count mismatch: ids={len(old_ids)}, expected={plan.old_record_count}"
        )

    delete_from_chroma(
        db_path,
        args.collection_name,
        old_ids,
        batch_size=args.delete_batch_size,
    )
    deleted_sqlite = delete_from_law_sqlite(
        sqlite_path,
        plan.old_source_files,
        batch_size=args.source_batch_size,
    )

    client = chromadb.PersistentClient(path=str(db_path))
    collection = client.get_collection(args.collection_name)
    remaining = collection.count()
    print("Dedup completed.", flush=True)
    print(f"Original article records: {plan.total_records}", flush=True)
    print(f"Deleted Chroma records: {len(old_ids)}", flush=True)
    print(f"Remaining Chroma records: {remaining}", flush=True)
    print(f"Deleted law SQLite records: {deleted_sqlite}", flush=True)
    print(f"Elapsed seconds: {time.time() - started:.1f}", flush=True)


if __name__ == "__main__":
    main()
