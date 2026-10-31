# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

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


def compress(src: str, out_dir: str) -> list[str]:
    """同时产出 .zst 与 .gz。

    两种都发不是冗余：下载端（tools/fetch_data.py）优先用 zst，但用户机器上
    可能既没有 zstandard 库也没有 zstd 命令，那时必须能退回 .gz —— gzip 是
    Python 内置，一定可用。只发 zst 会让这类用户彻底装不上数据。
    """
    outs: list[str] = []
    base = os.path.join(out_dir, os.path.basename(src))

    zstd = shutil.which("zstd")
    if zstd:
        dst_zst = base + ".zst"
        subprocess.run([zstd, "-19", "-f", "-q", src, "-o", dst_zst], check=True)
        outs.append(dst_zst)

    dst_gz = base + ".gz"
    with open(src, "rb") as fin, gzip.open(dst_gz, "wb", compresslevel=9) as fout:
        shutil.copyfileobj(fin, fout)
    outs.append(dst_gz)
    return outs


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

    db_archs = compress(db_copy, args.out_dir)
    db_sha = sha256(db_copy)
    os.remove(db_copy)

    artifacts = []
    for path in db_archs:
        artifacts.append({
            "name": os.path.basename(path),
            "sha256": sha256(path),
            "size": os.path.getsize(path),
            "kind": "full",
            "compression": "zstd" if path.endswith(".zst") else "gzip",
        })

    if args.delta:
        with open(args.delta, encoding="utf-8") as f:
            delta_meta = json.load(f)
        # 包里写的是「更新到哪一版」，必须和本次全量库的版本号一致。
        # 不一致说明增量包是用旧的库打的，用户应用后会拿到一个不存在的版本号，
        # 下次检查更新时还会再下全量——不如现在就拦下来。
        if delta_meta.get("to_version") != version:
            raise SystemExit(
                f"增量包的目标版本是 {delta_meta.get('to_version')}，"
                f"但本次全量库是 {version}。\n"
                "  增量包可能是用旧的库打的，重新生成一次再打包。"
            )
        for path in compress(args.delta, args.out_dir):
            artifacts.append({
                "name": os.path.basename(path),
                "sha256": sha256(path),
                "size": os.path.getsize(path),
                "kind": "delta",
                "compression": "zstd" if path.endswith(".zst") else "gzip",
                # 下载端靠 min_version 判断「本地库够不够新，能不能只补变更」。
                # 它不再是「必须精确等于上一版」：累积包里是每部法的最终态全文，
                # 只要本地不早于它就整部替换。
                "min_version": delta_meta.get("min_version"),
                "to_version": delta_meta.get("to_version"),
                "law_count": (delta_meta.get("summary") or {}).get("laws"),
                "article_count": (delta_meta.get("summary") or {}).get("articles"),
            })

    manifest = {
        "schema": 1,
        "schema_version": SCHEMA_VERSION,
        "corpus_version": version,
        "updated_at": updated_at,
        "law_count": laws,
        "article_count": articles,
        # 解压后未压缩库的哈希，比只看压缩包哈希更能证明数据没被换过
        "db_sha256": db_sha,
        "packaged_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "artifacts": artifacts,
    }
    manifest_path = os.path.join(args.out_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)

    print(f"发版资产已生成于 {args.out_dir}/")
    print(f"  版本 {version} ｜ 法规 {laws} 部 ｜ 法条 {articles} 条")
    for a in artifacts:
        print(f"  {a['name']}  {a['size'] / 1048576:.1f} MB  ({a['kind']})")
    print("\n上传命令：")
    print(f"  gh release create data-{version} {args.out_dir}/* --title \"数据 {version}\" "
          f"--notes \"法规 {laws} 部 / 法条 {articles} 条\"")


if __name__ == "__main__":
    main()
