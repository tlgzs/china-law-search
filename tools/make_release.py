"""打包发版：全量库 + 增量包 + manifest，生成可直接上传 GitHub Releases 的资产。

用法：
  python tools/make_release.py --db data/law_data.db --delta dist/delta-2026.10.30.2.json

产出（dist/）：
  law_data-<版本>.db.zst      全量库（无 zstd 时退化为 .gz）
  delta-<版本>.json.zst       增量包
  manifest.json               版本、条数、sha256、架构版本
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "ingest"))

from schema import ensure_schema, meta_get, SCHEMA_VERSION  # noqa: E402


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def compress(src: str, out_dir: str) -> str:
    zstd = shutil.which("zstd")
    if zstd:
        dst = os.path.join(out_dir, os.path.basename(src) + ".zst")
        subprocess.run([zstd, "-19", "-f", "-q", src, "-o", dst], check=True)
        return dst
    dst = os.path.join(out_dir, os.path.basename(src) + ".gz")
    with open(src, "rb") as fin, gzip.open(dst, "wb", compresslevel=9) as fout:
        shutil.copyfileobj(fin, fout)
    return dst


def main() -> None:
    ap = argparse.ArgumentParser(description="打包发版资产")
    ap.add_argument("--db", default="./data/law_data.db")
    ap.add_argument("--delta", default=None, help="本次增量包路径（可选）")
    ap.add_argument("--out-dir", default="./dist")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    ensure_schema(conn)
    version = meta_get(conn, "corpus_version", "0")
    articles = conn.execute("SELECT COUNT(*) FROM law_articles WHERE status != 'repealed'").fetchone()[0]
    laws = conn.execute(
        "SELECT COUNT(DISTINCT norm_name) FROM law_versions WHERE status = 'effective'"
    ).fetchone()[0]
    updated_at = meta_get(conn, "updated_at", "")
    conn.close()

    os.makedirs(args.out_dir, exist_ok=True)
    db_copy = os.path.join(args.out_dir, f"law_data-{version}.db")
    shutil.copyfile(args.db, db_copy)
    for suffix in ("-wal", "-shm"):
        side = args.db + suffix
        if os.path.exists(side):
            print(f"提示：{side} 存在，建议先执行 VACUUM 再打包")

    db_arch = compress(db_copy, args.out_dir)
    os.remove(db_copy)

    artifacts = [{"name": os.path.basename(db_arch), "sha256": sha256(db_arch),
                  "size": os.path.getsize(db_arch), "kind": "full"}]

    if args.delta:
        delta_arch = compress(args.delta, args.out_dir)
        artifacts.append({"name": os.path.basename(delta_arch), "sha256": sha256(delta_arch),
                          "size": os.path.getsize(delta_arch), "kind": "delta"})

    manifest = {
        "schema": 1,
        "schema_version": SCHEMA_VERSION,
        "corpus_version": version,
        "updated_at": updated_at,
        "law_count": laws,
        "article_count": articles,
        "packaged_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "artifacts": artifacts,
    }
    manifest_path = os.path.join(args.out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    print(f"发版资产已生成于 {args.out_dir}/")
    print(f"  版本 {version} ｜ 法规 {laws} 部 ｜ 法条 {articles} 条")
    for a in artifacts:
        print(f"  {a['name']}  {a['size'] / 1048576:.1f} MB")
    print("\n上传命令示例：")
    print(f"  gh release create data-{version} {args.out_dir}/*.zst {args.out_dir}/*.gz "
          f"{manifest_path} --title \"数据 {version}\" --notes \"法规 {laws} 部 / 法条 {articles} 条\"")


if __name__ == "__main__":
    main()
