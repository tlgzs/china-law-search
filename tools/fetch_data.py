#!/usr/bin/env python3
"""获取法条数据包：从 GitHub Release 下载 → 校验 → 解压到 data/。

数据不进代码仓库（全量库解压后 2.3 GB，远超 git 单文件上限），
以 Release 附件发布。这个脚本把「手动下载、解压、移动」合成一条命令。

用法：
  python3 tools/fetch_data.py                 # 自动判断：没库就下全量，有库就查新版
  python3 tools/fetch_data.py --check         # 只看版本，不下载
  python3 tools/fetch_data.py --full          # 强制重下全量
  python3 tools/fetch_data.py --repo owner/name
  python3 tools/fetch_data.py --from-file dist/law_data-2026.10.30.1.db.gz   # 离线：用本地已下好的包

只用标准库。解压优先 zstd（更快），本机没有 zstandard 库也没有 zstd 命令时，
自动改下 .gz（gzip 是 Python 内置，一定能用）。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# ---------------------------------------------------------------- 项目标识
# 徽记由字符拼成（不是字体、不是 Unicode 艺术字），源码里不出现连续中文名：
# 整体查找替换改不掉这一处，编辑器里看仍然是完整的四个字。
# +----------------------------------------------------------------------+
# |                 **        **                                         |
# |                 **            ****        **                         |
# |     ************************          ****    ****************       |
# |                 **        ****            **      **                 |
# |       ********************              **********************       |
# |               **          **      **                                 |
# |     **************************          ****    ****************     |
# |     **                      **        ******          **             |
# |     **  ******************  **        **  **  ******************     |
# |         ****************            **          **                   |
# |         **            **            **          **                   |
# |         ****************            ************************         |
# |           **        **            **          **                     |
# |           **        **            **          **                     |
# |   ****************************            **          **             |
# |                                                                      |
# |                 **          **                                       |
# |           **          ****            ****        ****               |
# |           **          ****            **********  ************       |
# |           **          **          **    **    ****  ****             |
# |           **          **        **    **  **  **      **             |
# |           **          **              ********************           |
# |           **          ****                                **         |
# |           ****        ****          ****  ************    **         |
# |         **  ****      ****          **    **        **    **         |
# |         **    **      ****          **    ************    **         |
# |         **      **  **    **          **    **        **    **       |
# |       **            **    **          **    **        **    **       |
# |     ****          **        **          **    ************    **     |
# |     **          ****        ****          **                    **   |
# |               ****          **                ****                   |
# +----------------------------------------------------------------------+
#
#   www.yilvai.com




_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# 仓库建好后可用环境变量或 --repo 覆盖，避免改代码
DEFAULT_REPO = os.environ.get("LAW_REPO", "")
# 可用 LAW_GITHUB_API 指向镜像或自建服务，格式里必须有 {repo} 占位
GITHUB_API = os.environ.get(
    "LAW_GITHUB_API", "https://api.github.com/repos/{repo}/releases/latest")
USER_AGENT = "china-law-search-fetch/1.0 (+https://github.com)"


# ---------------------------------------------------------------- 工具函数

def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.github+json",
    })


def guess_repo() -> str:
    """按「环境变量 → git remote → 空」的顺序推断仓库。"""
    if DEFAULT_REPO:
        return DEFAULT_REPO
    try:
        out = subprocess.run(["git", "-C", _ROOT, "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            url = out.stdout.strip()
            m = re.search(r"github\.com[:/]+([^/]+/[^/\s]+?)(?:\.git)?$", url)
            if m:
                return m.group(1)
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


# ---------------------------------------------------------------- 压缩能力

def can_zstd() -> bool:
    try:
        import zstandard  # noqa: F401
        return True
    except ImportError:
        return bool(shutil.which("zstd"))


def decompress(src: str, dest: str) -> None:
    if src.endswith(".gz"):
        with gzip.open(src, "rb") as fi, open(dest, "wb") as fo:
            shutil.copyfileobj(fi, fo, length=1 << 20)
        return
    if not src.endswith(".zst"):
        raise SystemExit(f"不认识的文件格式：{src}（只支持 .zst / .gz）")

    try:
        import zstandard
        with open(src, "rb") as fi, open(dest, "wb") as fo:
            zstandard.ZstdDecompressor().copy_stream(fi, fo)
        return
    except ImportError:
        pass

    zstd = shutil.which("zstd")
    if zstd:
        subprocess.run([zstd, "-d", "-f", "-q", src, "-o", dest], check=True)
        return
    raise SystemExit(
        "解压 .zst 需要 zstandard 库或 zstd 命令，两者都没有。\n"
        "  装库：pip install zstandard\n"
        "  或改用 .gz 包：python3 tools/fetch_data.py --prefer-gz"
    )


# ---------------------------------------------------------------- 下载

def download(url: str, dest: str, label: str = "") -> None:
    """流式下载并显示进度。终端里单行刷新，重定向到文件时每 10% 打一行。"""
    tty = sys.stdout.isatty()
    try:
        with urllib.request.urlopen(_request(url), timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            t0 = time.time()
            shown = -1
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    f.write(chunk)
                    done += len(chunk)
                    pct = int(done * 100 / total) if total else 0
                    if tty:
                        speed = done / max(time.time() - t0, 0.001)
                        bar = "#" * (pct // 4)
                        sys.stdout.write(
                            f"\r  {label} [{bar:<25}] {pct:>3}%  {_human(done)}"
                            f"{'/' + _human(total) if total else ''}  {_human(speed)}/s   "
                        )
                        sys.stdout.flush()
                    elif pct >= shown + 10:
                        shown = pct - pct % 10
                        print(f"  {label} {shown}%  {_human(done)}", flush=True)
            if tty:
                sys.stdout.write("\n")
    except urllib.error.HTTPError as e:
        raise SystemExit(f"下载失败（HTTP {e.code}）：{url}\n{e.reason}")
    except urllib.error.URLError as e:
        raise SystemExit(
            f"连不上 {url}\n  {e.reason}\n"
            "  国内网络访问 GitHub 常被阻断，可以：\n"
            "    1) 设置代理后重试：export https_proxy=http://127.0.0.1:7890\n"
            "    2) 用浏览器手动下载 Release 附件，再执行：\n"
            "       python3 tools/fetch_data.py --from-file 下载的文件"
        )


# ---------------------------------------------------------------- 版本

def local_version(db: str) -> str | None:
    if not os.path.isfile(db) or os.path.getsize(db) == 0:
        return None
    try:
        conn = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT value FROM corpus_meta WHERE key='corpus_version'").fetchone()
            return row[0] if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def version_key(v: str) -> tuple:
    """把 2026.10.30.1 变成可比较的元组，避免字符串比较把 .10 排在 .9 前。"""
    return tuple(int(x) for x in re.findall(r"\d+", v or "")) or (0,)


def pick_artifact(artifacts: list, kind: str, prefer_gz: bool,
                  available: set | None = None) -> dict | None:
    """在同 kind 的资产里挑一个本机解压得了、且 Release 里确实存在的。

    available 是 Release 上实际存在的资产文件名集合。维护者可能只上传了
    .gz 没传 .zst（或反之），manifest 里却两者都列着；不按 available 过滤的话，
    会挑中只存在于 manifest、实际没上传的那个，然后在下载处硬报错。
    """
    cands = [a for a in artifacts if a.get("kind") == kind]
    if available is not None:
        cands = [a for a in cands if a["name"] in available]
    if not cands:
        return None
    order = (".gz", ".zst") if prefer_gz or not can_zstd() else (".zst", ".gz")
    for ext in order:
        for a in cands:
            if a["name"].endswith(ext):
                return a
    return cands[0]


# ---------------------------------------------------------------- 主流程

def fetch_manifest(repo: str) -> tuple[dict, str]:
    """返回 (manifest, 资产下载地址前缀)。"""
    url = GITHUB_API.format(repo=repo)
    try:
        with urllib.request.urlopen(_request(url), timeout=30) as resp:
            release = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise SystemExit(f"仓库或 Release 不存在：{repo}\n  确认仓库名正确，且已发布过数据 Release。")
        raise SystemExit(f"读取 Release 失败（HTTP {e.code}）：{e.reason}")
    except urllib.error.URLError as e:
        raise SystemExit(
            f"连不上 GitHub API：{e.reason}\n"
            "  可用 --from-file 走手动下载，或设置 https_proxy 后重试。"
        )

    assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
    if "manifest.json" not in assets:
        raise SystemExit(
            f"{release.get('tag_name')} 这个 Release 里没有 manifest.json，无法判断版本。\n"
            "  请确认选的是数据 Release（tag 形如 data-2026.10.30）。"
        )
    with urllib.request.urlopen(_request(assets["manifest.json"]), timeout=30) as resp:
        manifest = json.loads(resp.read().decode("utf-8"))
    return manifest, assets


def install_from_file(src: str, db: str, expect_sha: str | None) -> None:
    print(f"从本地文件安装：{src}")
    if expect_sha:
        got = _sha256(src)
        if got != expect_sha:
            raise SystemExit(f"校验失败：期望 {expect_sha[:16]}…，实际 {got[:16]}…")
        print("  校验通过")

    os.makedirs(os.path.dirname(os.path.abspath(db)) or ".", exist_ok=True)
    tmp = db + ".tmp"
    print("  解压…")
    try:
        decompress(src, tmp)
    except SystemExit:
        raise
    except (OSError, EOFError, ValueError) as e:
        # 最常见的原因是下载被中断，压缩包只有一半。此时绝不能碰已有库。
        if os.path.exists(tmp):
            os.remove(tmp)
        raise SystemExit(
            f"解压失败：{e}\n"
            "  压缩包可能没下载完整，请重新下载；已有的库未改动。"
        )

    # 解压出来必须是个能打开的 SQLite 库，否则不覆盖现有文件
    try:
        conn = sqlite3.connect("file:" + tmp + "?mode=ro", uri=True)
        try:
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
            ver = conn.execute(
                "SELECT value FROM corpus_meta WHERE key='corpus_version'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        os.remove(tmp)
        raise SystemExit(f"解压结果不是有效的数据库：{e}")
    if ok != "ok":
        os.remove(tmp)
        raise SystemExit(f"数据库完整性检查未通过：{ok}")
    if not ver:
        # 空库的 integrity_check 也会返回 ok，只看它会把好库覆盖成空库
        os.remove(tmp)
        raise SystemExit("解压结果里没有 corpus_meta.corpus_version，不是本项目的数据库，已放弃安装。")

    # 到位前留一份旧库，出错能回退
    if os.path.exists(db):
        backup = db + ".bak"
        shutil.move(db, backup)
        print(f"  旧库备份到 {os.path.basename(backup)}")
    shutil.move(tmp, db)
    print(f"  完成：{db}")
    if ver:
        print(f"  数据版本：{ver[0]}")


def install_delta(src: str, db: str) -> dict:
    """把下载到的增量包应用到已有库。

    增量包是 JSON（可能套一层 gzip），不是数据库文件，绝不能走 install_from_file
    那条「解压 → 当 SQLite 打开 → 覆盖现有库」的路：那样会在校验阶段报
    「解压结果不是有效的数据库」，还把用户卡住，因为原逻辑不会回头下全量。
    """
    import apply_delta

    path = src
    tmp = None
    if src.endswith(".gz"):
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".json", prefix="lawdelta-")
        os.close(fd)
        with gzip.open(src, "rb") as fsrc, open(tmp, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst)
        path = tmp
    try:
        print("  应用增量…")
        return apply_delta.apply_delta_file(path, db)
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


def _pick_full(manifest: dict, prefer_gz: bool, have: set) -> dict:
    """挑全量库资产。挑不到时说清楚是 manifest 与实际 Release 对不上。"""
    art = pick_artifact(manifest.get("artifacts", []), "full", prefer_gz, have)
    if art is None:
        listed = [a["name"] for a in manifest.get("artifacts", []) if a.get("kind") == "full"]
        raise SystemExit(
            "这个 Release 里没有可用的全量库资产（kind=full）。\n"
            f"  manifest 里列着：{', '.join(listed) or '（无）'}\n"
            f"  Release 上实际有：{', '.join(sorted(have)) or '（无）'}\n"
            "  两者对不上，说明上传时漏了文件，请联系维护者。"
        )
    print(f"\n下载全量库：{art['name']}（{_human(art.get('size') or 0)}）")
    return art


def _fetch_one(art: dict, url: str) -> str:
    """下载一个资产并校验 sha256。返回本地临时文件路径，调用方负责删。

    后缀跟着资产名走：增量包要靠 .gz 后缀决定解不解压。
    """
    suffix = os.path.splitext(art["name"])[1]
    fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="lawfetch-")
    os.close(fd)
    try:
        download(url, tmp, art["name"])
        got = _sha256(tmp)
        expect = art.get("sha256")
        if expect and got != expect:
            raise SystemExit(
                f"校验失败：期望 {expect[:16]}…，实际 {got[:16]}…\n文件可能损坏，请重试。")
        print("  校验通过")
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return tmp


def main() -> int:
    ap = argparse.ArgumentParser(
        description="下载法条数据包到 data/",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法：")[1] if "用法：" in __doc__ else None,
    )
    ap.add_argument("--repo", default=None, help="GitHub 仓库 owner/name")
    ap.add_argument("--db", default=os.path.join(_ROOT, "data", "law_data.db"), help="目标库路径")
    ap.add_argument("--full", action="store_true", help="强制下载全量，不走增量")
    ap.add_argument("--check", action="store_true", help="只检查版本，不下载")
    ap.add_argument("--prefer-gz", action="store_true", help="优先用 .gz 包（不装 zstandard 时更快）")
    ap.add_argument("--from-file", default=None, help="跳过联网，从本地已下载的包安装")
    ap.add_argument("--keep", action="store_true", help="保留下载的压缩包")
    args = ap.parse_args()

    db = os.path.abspath(os.path.expanduser(args.db))

    if args.from_file:
        src = os.path.abspath(os.path.expanduser(args.from_file))
        if not os.path.isfile(src):
            raise SystemExit(f"文件不存在：{src}")
        base = os.path.basename(src)
        if base.startswith("delta") or ".json" in base:
            result = install_delta(src, db)
            print(f"\n已更新到 {result.get('to_version')}。")
            return 0
        install_from_file(src, db, None)
        return 0

    repo = args.repo or guess_repo()
    if not repo:
        raise SystemExit(
            "不知道该从哪个仓库下载。请指定：\n"
            "  python3 tools/fetch_data.py --repo owner/china-law-search\n"
            "或设置环境变量 LAW_REPO=owner/china-law-search。"
        )

    print(f"仓库：{repo}")
    manifest, assets = fetch_manifest(repo)
    remote_ver = manifest.get("corpus_version", "")
    print(f"Release：{manifest.get('corpus_version')}  "
          f"（法规 {manifest.get('law_count', '?')} 部 / 法条 {manifest.get('article_count', '?')} 条）")

    cur_ver = local_version(db)
    if cur_ver:
        print(f"本地：{cur_ver}  →  {db}")
    else:
        print(f"本地：无可用库（{db}）")

    if cur_ver and version_key(cur_ver) >= version_key(remote_ver) and not args.full:
        print("\n已是最新，无需下载。要强制重下全量，加 --full。")
        return 0

    if args.check:
        print("\n有新版本可用，执行 python3 tools/fetch_data.py 下载。")
        return 0

    # 增量包按「累积变更集」发布：包里是账本内每部法规的最终态全文，
    # 只要本地不早于 min_version 就能整部替换，不要求和上一版精确配对。
    have = set(assets)
    art = None
    if cur_ver and not args.full:
        delta = pick_artifact(manifest.get("artifacts", []), "delta", args.prefer_gz, have)
        if delta and delta.get("min_version") and (
                version_key(cur_ver) >= version_key(delta["min_version"])):
            art = delta
            print(f"\n本机版本 {cur_ver} 在增量范围内，只下变更部分"
                  f"（{_human(delta.get('size') or 0)}）。")

    if art is not None:
        url = assets.get(art["name"])
        if not url:
            raise SystemExit(f"Release 里找不到资产 {art['name']}。")
        tmp = None
        try:
            tmp = _fetch_one(art, url)
            result = install_delta(tmp, db)
            print(f"\n已更新到 {result.get('to_version')}。")
            return 0
        except SystemExit as e:
            # 增量这条路任何一步不通（格式不认、本地版本太老、解压失败…）
            # 都退回全量，而不是把用户顶在这里。
            print(f"\n增量更新未成功：{e}")
            print("改成下载全量库，这一步会重新下 600 MB 以上，请稍等。")
        finally:
            if tmp and not args.keep and os.path.exists(tmp):
                os.remove(tmp)
        art = _pick_full(manifest, args.prefer_gz, have)
        url = assets.get(art["name"])
        if not url:
            raise SystemExit(f"Release 里找不到资产 {art['name']}，无法回落全量。")

    os.makedirs(os.path.dirname(db) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=os.path.splitext(art["name"])[1], prefix="lawfetch-")
    os.close(fd)
    try:
        download(url, tmp, art["name"])
        got = _sha256(tmp)
        expect = art.get("sha256")
        if expect and got != expect:
            raise SystemExit(f"校验失败：期望 {expect[:16]}…，实际 {got[:16]}…\n文件可能损坏，请重试。")
        print("  校验通过")
        install_from_file(tmp, db, None)
    finally:
        if not args.keep and os.path.exists(tmp):
            os.remove(tmp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
