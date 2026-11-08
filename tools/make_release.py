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
import tempfile
from datetime import datetime, timezone


_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "ingest"))
sys.path.insert(0, _HERE)

from schema import ensure_schema, meta_get, SCHEMA_VERSION  # noqa: E402
from release_gate import FAIL, GATES, run, _cmd_make_delta  # noqa: E402

_ROOT = os.path.dirname(_HERE)

# 门禁分两段：库侧的在打包前跑，产物侧的在 manifest 写完之后跑。
# 这里没有、也不要加任何跳过开关 —— 放行只能靠人工改这个文件，git 留痕。
_PRE_GATES = ["G1", "G2", "G3", "G4", "G5", "G6", "G7"]
_POST_GATES = ["G8", "G9", "G10"]


def _g7_registry_path() -> str | None:
    """G7 豁免清单：与 release_gate CLI 的默认路径同源。

    必须显式传入，否则 run() 会按「无清单」处理，把所有源缺号/源侧超长都当成阻断，
    与 CLI 单独跑 G7 的结果不一致（本轮曾因此在打包前被误拦）。
    """
    path = os.path.join(_ROOT, "reports", "g7-gap-registry-20261107.csv")
    return path if os.path.exists(path) else None


def _catalog_path() -> str | None:
    """应收录清单只在文件真实存在时启用。

    G5 的清单比对需要「应处理全集」，这个产物还没有（见 docs/发布门禁-20261105.md），
    所以这里不做任何兜底伪造：没有文件时 G5 只跑批次判定，清单比对跳过。
    """
    path = os.path.join(_ROOT, "reports", "catalog.csv")
    return path if os.path.exists(path) else None


def _gate_stage(gate_ids: list[str], *, db: str, manifest: str | None,
                artifacts_dir: str | None, previous_manifest: str | None,
                stage: str, extra_artifacts_dirs: list[str] | None = None) -> None:
    """跑一段门禁，有阻断项就打印并退出 1。

    失败必须先于任何产出发生（打包前）或先于任何落盘发生（打包后写的是临时目录），
    所以这里只负责判定和打印，不清理也不回滚——调用方保证那时还没有覆盖任何资产。
    """
    results = run(
        db, manifest, os.path.join(_ROOT, "VERSION"), artifacts_dir,
        None, _catalog_path(), previous_manifest,
        datetime.now().date().isoformat(), gate_ids, _g7_registry_path(),
        extra_artifacts_dirs,
    )
    blocked = [r for r in results if r.status == FAIL]
    if not blocked:
        return
    print(f"\n发布门禁未通过（{stage}），已中止，未生成/覆盖任何资产：")
    for r in blocked:
        print(f"  [{r.gate}] {GATES[r.gate][0]}｜{r.message}")
        for sample in r.samples:
            print(f"      · {sample}")
    print("  门禁不提供跳过参数；确需放行只能人工修改 tools/make_release.py（git 留痕）。")
    raise SystemExit(1)


def _preflight_assets(version: str, delta_arg: str | None, out_dir: str) -> str:
    """压缩开始前的轻量预检，返回本次要打包的增量包路径。

    全量包与增量包的压缩变体是打包过程自己生成的，这里查不到也不必查；真正
    会在跑了 2.4 GB 压缩之后才炸的是三样：delta 源文件不存在、delta 的
    to_version 与库不符、变更账本没更新到本次版本。三样都在这里一次报全，
    别让用户白等一轮打包。
    """
    delta_path = delta_arg or os.path.join(out_dir, f"delta-{version}.json")
    problems: list[str] = []

    if not os.path.isfile(delta_path) or os.path.getsize(delta_path) == 0:
        problems.append(
            f"增量包缺失：{delta_path} 不存在或为空（全量与增量必须一起发，缺一不发）。"
            f"修复：先把本轮 plan 并入账本再出包 —— {_cmd_make_delta(version)}")
    else:
        delta_meta: dict | None = None
        try:
            with open(delta_path, encoding="utf-8") as f:
                delta_meta = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"增量包无法解析：{delta_path}（{exc}）")
        if delta_meta is not None:
            if delta_meta.get("to_version") != version:
                problems.append(
                    f"增量包目标版本不符：{delta_meta.get('to_version')} / 本次库 {version}。"
                    f"修复：用当前库重新出包 —— {_cmd_make_delta(version)}")
            if not (delta_meta.get("summary") or {}).get("laws"):
                problems.append(
                    f"增量包内法规数为 0，拒绝发布空增量。修复：{_cmd_make_delta(version)}")

    ledger_path = os.path.join(_ROOT, "state", "change-ledger.json")
    if not os.path.exists(ledger_path):
        problems.append(f"变更账本不存在：{ledger_path}。修复：{_cmd_make_delta(version)}")
    else:
        try:
            with open(ledger_path, encoding="utf-8") as f:
                ledger = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            ledger = {}
            problems.append(f"变更账本读取失败：{ledger_path}（{exc}）")
        ledger_version = (ledger.get("corpus_version") or "").strip()
        if not ledger_version:
            problems.append(
                "变更账本缺 corpus_version 字段，无法确认它已并入本次版本。"
                f"修复：{_cmd_make_delta(version)}（make_delta 写回时会带上目标版本）")
        elif ledger_version != version:
            problems.append(
                f"变更账本未更新到本次版本：账本 {ledger_version} / 库 {version}。"
                f"修复：把本轮 plan 并入账本后重跑 —— {_cmd_make_delta(version)}")

    if problems:
        print("\n发布前置检查未通过（尚未开始压缩，未生成/覆盖任何资产）：")
        for p in problems:
            print(f"  · {p}")
        print("  这条检查不提供跳过参数；全量与增量必须成对发布。")
        raise SystemExit(1)
    return delta_path


def app_version() -> str:
    """程序版本，取自仓库根目录 VERSION。

    写进 manifest 是为了让已装的用户能一眼对上「这份数据包是哪个程序版本发的」，
    省得出了问题还得翻 release 页面猜。程序新版本的判断走 app-v* tag，不看这个字段。
    """
    try:
        with open(os.path.join(_ROOT, "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


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
    ap.add_argument("--delta", default=None,
                    help="本次增量包路径（缺省取 <out-dir>/delta-<版本>.json；全量与增量必须成对发布）")
    ap.add_argument("--out-dir", default="./dist")
    args = ap.parse_args()

    # 入口预检：只读开库取版本号，先查增量包与账本是否就位。放在打包前门禁
    # 之前——G5 要扫全库，等它跑完才发现缺 delta 等于白跑一轮。
    probe = sqlite3.connect(f"file:{os.path.abspath(args.db)}?mode=ro", uri=True)
    try:
        entry_version = meta_get(probe, "corpus_version", "0")
    finally:
        probe.close()
    delta_path = _preflight_assets(entry_version, args.delta, args.out_dir)

    # 打包前门禁必须在 ensure_schema 之前：ensure_schema 会按「今天」推进 pending
    # 状态，跑在它后面就把 G1 要查的矛盾自己抹平了。
    _gate_stage(_PRE_GATES, db=args.db, manifest=None, artifacts_dir=None,
                previous_manifest=None, stage="打包前 preflight")

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
    # 所有产物先写进 out_dir 下的临时目录，等 G8/G9 通过后再用 os.replace 落正式路径。
    # 这样任何一道门禁失败时，已发布的旧资产一个字节都不会被碰到。
    staging = tempfile.mkdtemp(prefix=".release-staging-", dir=args.out_dir)
    try:
        db_copy = os.path.join(staging, f"law_data-{version}.db")
        # 用硬链接而不是真复制：全量库 2.4 GB，压缩前再复制一份意味着打包要吃掉
        # 额外 2.4 GB 磁盘——发版机器磁盘紧张时这一步会直接把空间撑爆。硬链接和
        # 原库指向同一份数据块，不占空间，压完删链接也不影响原库。
        # 跨文件系统（比如 output 指到外置盘）时硬链接会失败，退回真复制。
        try:
            if os.path.exists(db_copy):
                os.remove(db_copy)
            os.link(args.db, db_copy)
            linked = True
        except OSError:
            shutil.copyfile(args.db, db_copy)
            linked = False
        for suffix in ("-wal", "-shm"):
            side = args.db + suffix
            if os.path.exists(side):
                print(f"提示：{side} 存在，建议先执行 VACUUM 再打包")
        if not linked:
            print("提示：硬链接不可用（跨文件系统），已改为复制，注意预留磁盘空间")

        db_archs = compress(db_copy, staging)
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

        if delta_path:
            with open(delta_path, encoding="utf-8") as f:
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
            for path in compress(delta_path, staging):
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
                    # 整部废止的部数。归到新增/修改之外单列：用户看到的「本次变更
                    # N 部」若把废止算进去，会以为多了几部新法，实际是少了几部。
                    "repealed_count": (delta_meta.get("summary") or {}).get("repealed") or 0,
                })

        manifest = {
            "schema": 1,
            "schema_version": SCHEMA_VERSION,
            "app_version": app_version(),
            "corpus_version": version,
            "updated_at": updated_at,
            "law_count": laws,
            "article_count": articles,
            # 解压后未压缩库的哈希，比只看压缩包哈希更能证明数据没被换过
            "db_sha256": db_sha,
            "packaged_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "artifacts": artifacts,
        }
        manifest_path = os.path.join(staging, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)

        # 打包后门禁：校验刚生成的 manifest 与实际产物。上一版 manifest 一起传进去，
        # 库变了却没 bump corpus_version 会被当场拦下。
        previous_manifest = os.path.join(args.out_dir, "manifest.json")
        _gate_stage(_POST_GATES, db=args.db, manifest=manifest_path,
                    artifacts_dir=staging,
                    previous_manifest=previous_manifest if os.path.exists(previous_manifest) else None,
                    stage="打包后 / 发布前校验",
                    # staging 里只有压缩变体，原始 delta json 留在 out_dir，两个目录都要查
                    extra_artifacts_dirs=[staging, args.out_dir])

        # 门禁全过才落正式路径：先产物、最后 manifest，避免出现指向缺失文件的 manifest。
        for item in artifacts:
            os.replace(os.path.join(staging, item["name"]),
                       os.path.join(args.out_dir, item["name"]))
        os.replace(manifest_path, os.path.join(args.out_dir, "manifest.json"))
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    print(f"发版资产已生成于 {args.out_dir}/")
    print(f"  版本 {version} ｜ 法规 {laws} 部 ｜ 法条 {articles} 条")
    for a in artifacts:
        print(f"  {a['name']}  {a['size'] / 1048576:.1f} MB  ({a['kind']})")
    print("\n上传命令：")
    print(f"  gh release create data-{version} {args.out_dir}/* --title \"数据 {version}\" "
          f"--notes \"法规 {laws} 部 / 法条 {articles} 条\"")


if __name__ == "__main__":
    main()
