#!/usr/bin/env python3
# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""获取法条数据包：从 GitHub Release 下载 → 校验 → 解压到 data/。

数据不进代码仓库（全量库解压后 2.4 GB，远超 git 单文件上限），
以 Release 附件发布。这个脚本把「手动下载、解压、移动」合成一条命令。

用法：
  python3 tools/fetch_data.py                 # 自动判断：没库就下全量，有库就查新版
  python3 tools/fetch_data.py --check         # 只看版本，不下载
  python3 tools/fetch_data.py --full          # 强制重下全量
  python3 tools/fetch_data.py --repo owner/name
  python3 tools/fetch_data.py --from-file dist/law_data-2026.10.30.1.db.gz   # 离线：用本地已下好的包

国内镜像（按顺序尝试，GitHub 永远最后兜底）：
  export LAW_DATA_MIRRORS="https://gitee.com/<账号>/<仓库>/raw/master https://cdn.example.com/law-data"
每个镜像目录里放 manifest.json 和发布资产，文件名与 manifest 里的 artifacts[].name 一致。
每个源都按 manifest 里的 sha256 校验，校验不过视为该源失败，自动换下一个。

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
import urllib.parse
import urllib.request


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

# 当前进行到哪一步。安装中途失败时，用户最需要知道的就是「卡在哪」而不是一段栈。
_STEP = "准备"


# ---------------------------------------------------------------- 工具函数

def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _human_time(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec:02d} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes:02d} 分"


def _step(name: str) -> None:
    global _STEP
    _STEP = name


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

# 资产名 → api.github.com 的资产地址。
# browser_download_url 走 github.com/releases/download/…，而只放行 api 域名的网络
# （企业代理、部分校园网）会把这条打回超时，此时用 API 资产接口 + octet-stream
# 能拿到同样的字节。fetch_manifest 每次刷新这张表。
_ASSET_API: dict[str, str] = {}


def _asset_api_request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        # 必须 octet-stream：默认的 vnd.github+json 会返回资产的元数据 JSON，
        # 而不是文件本身。
        "Accept": "application/octet-stream",
    })


def _stream(url: str, dest: str, label: str, req: urllib.request.Request) -> None:
    """流式写盘并显示「已下载 / 总量 / 速度 / 预计剩余」。

    708 MB 这一步只给一个百分比太干；速度能让人判断要不要等，ETA 能让人决定
    先去干别的。非交互环境（日志、CI）每 10% 打一行，别刷屏。
    """
    tty = sys.stdout.isatty()
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        t0 = time.time()
        last_draw = 0.0
        shown_pct = -10
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                now = time.time()
                elapsed = max(now - t0, 0.001)
                speed = done / elapsed
                pct = int(done * 100 / total) if total else 0
                eta = (total - done) / speed if total and speed > 0 else 0
                if tty:
                    if now - last_draw < 0.3 and total:
                        continue
                    last_draw = now
                    bar = "#" * (pct // 4)
                    left = f"  剩余 {_human_time(eta)}" if eta else ""
                    sys.stdout.write(
                        f"\r  {label} [{bar:<25}] {pct:>3}%  {_human(done)}"
                        f"{'/' + _human(total) if total else ''}"
                        f"  {_human(speed)}/s{left}   "
                    )
                    sys.stdout.flush()
                elif pct >= shown_pct + 10:
                    shown_pct = pct - pct % 10
                    size = f"{_human(done)} / {_human(total)}" if total else _human(done)
                    left = f"  剩余 {_human_time(eta)}" if eta else ""
                    print(f"  {label} 已下载 {size} ({pct}%)  {_human(speed)}/s{left}",
                          flush=True)
        elapsed = max(time.time() - t0, 0.001)
        if tty:
            sys.stdout.write("\n")
        print(f"  下载完成：{_human(done)}，用时 {_human_time(elapsed)}，"
              f"平均 {_human(done / elapsed)}/s", flush=True)


def download(url: str, dest: str, label: str = "") -> None:
    """下载到 dest。第一跳失败时换 API 资产地址再试一次。"""
    try:
        _stream(url, dest, label, _request(url))
        return
    except urllib.error.HTTPError as e:
        first_err: BaseException = SystemExit(f"下载失败（HTTP {e.code}）：{url}\n{e.reason}")
    except (urllib.error.URLError, OSError) as e:
        # OSError 覆盖读流超时（socket.timeout）和中途断连：这类错误以前会直接抛栈。
        reason = getattr(e, "reason", None) or e
        first_err = SystemExit(f"连不上 {url}\n  {reason}")

    api_url = _ASSET_API.get(label)
    if not api_url or api_url == url:
        raise first_err
    print(f"  {label} 直连失败，改用备用地址重试…")
    try:
        _stream(api_url, dest, label, _asset_api_request(api_url))
    except (urllib.error.HTTPError, urllib.error.URLError, OSError):
        raise first_err


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
    _ASSET_API.clear()
    for a in release.get("assets", []):
        if a.get("name") and a.get("url"):
            _ASSET_API[a["name"]] = a["url"]
    if "manifest.json" not in assets:
        raise SystemExit(
            f"{release.get('tag_name')} 这个 Release 里没有 manifest.json，无法判断版本。\n"
            "  请确认选的是数据 Release（tag 形如 data-2026.10.30）。"
        )
    manifest = _read_asset_json("manifest.json", assets["manifest.json"])
    return manifest, assets


def _read_asset_json(name: str, url: str) -> dict:
    """读一个小 JSON 资产（manifest.json，几百字节）。

    优先走 API 资产接口，再走 browser_download_url。顺序是反的，原因在等待时间：
    github.com 不通的网络上，先试下行地址要干等 30 秒超时才能回退，用户会以为
    「检查更新卡死了」。API 接口在两种网络下都通，且 manifest 只有几百字节，
    走 API 没有任何速度损失。大文件相反 —— 下行地址更快，见 download()。
    """
    api_url = _ASSET_API.get(name)
    if api_url and api_url != url:
        try:
            with urllib.request.urlopen(_asset_api_request(api_url), timeout=10) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            pass
    with urllib.request.urlopen(_request(url), timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------- 下载源

def mirror_bases() -> list[str]:
    """LAW_DATA_MIRRORS（兼容单数 LAW_DATA_MIRROR）里的镜像目录，保持用户顺序。

    目录里要放 manifest.json 和发布资产，资产名与 artifacts[].name 一致。
    分隔符认逗号、分号、空白，用户从聊天窗口复制来的串通常混着这几种。
    """
    raw = os.environ.get("LAW_DATA_MIRRORS") or os.environ.get("LAW_DATA_MIRROR") or ""
    return [b.strip().rstrip("/") for b in re.split(r"[,;\s]+", raw) if b.strip()]


def _mirror_request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "*/*",   # 静态文件服务器不认 GitHub 的媒体类型
    })


def load_mirror(base: str) -> tuple[str, dict, dict] | None:
    """读镜像目录里的 manifest.json。失败只警告并返回 None，由调用方换下一个源。"""
    label = f"镜像 {base}"
    url = f"{base}/manifest.json"
    try:
        with urllib.request.urlopen(_mirror_request(url), timeout=15) as resp:
            manifest = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"  跳过 {label}：manifest 读取失败（HTTP {e.code}）")
        return None
    except (urllib.error.URLError, OSError, ValueError) as e:
        reason = getattr(e, "reason", None) or e
        print(f"  跳过 {label}：{reason}")
        return None
    if not isinstance(manifest, dict) or not manifest.get("corpus_version"):
        print(f"  跳过 {label}：manifest.json 格式不对（缺 corpus_version）")
        return None

    assets = {"manifest.json": url}
    for art in manifest.get("artifacts", []):
        if art.get("name"):
            # 资产默认按「与 manifest 同目录」拼。清单和增量放 Gitee、全量放对象存储
            # 这种组合，可以在 manifest 里给单个资产写绝对地址（artifacts[].url）。
            assets[art["name"]] = art.get("url") or f"{base}/{urllib.parse.quote(art['name'])}"
    return label, manifest, assets


def load_github(repo: str) -> tuple[str, dict, dict] | None:
    """GitHub 永远作为最后一个源。失败只警告，不打断其它镜像。"""
    try:
        manifest, assets = fetch_manifest(repo)
    except SystemExit as e:
        print(f"  跳过 GitHub：{e}")
        return None
    return f"GitHub {repo}", manifest, assets


def _all_sources_failed(tried: list[str]) -> str:
    return (
        "所有下载源都失败了，没有安装任何数据。\n"
        f"  已尝试：{'、'.join(tried) or '（无）'}\n"
        "\n"
        "可以这样处理：\n"
        "  1) 配置国内镜像后重试（按顺序尝试，GitHub 永远最后兜底）：\n"
        '     export LAW_DATA_MIRRORS="https://gitee.com/<账号>/<仓库>/raw/master"\n'
        "  2) 浏览器打开官方 Release 页手动下载：\n"
        "     https://github.com/tlgzs/china-law-search/releases/latest\n"
        "     然后：python3 tools/fetch_data.py --from-file 下载到的文件\n"
        "  3) 有代理的话：export https_proxy=http://127.0.0.1:7890 后重试"
    )


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

    增量包是 JSON，外面可能套一层 gzip 或 zstd。两种都要解，不能只认 .gz：
    下载端优先挑 .zst（体积小一半），只处理 .gz 会让所有装了 zstd 的机器在
    json.load 那一步炸成 UnicodeDecodeError。

    也不能走 install_from_file 那条「解压 → 当 SQLite 打开 → 覆盖现有库」的路：
    那样会在校验阶段报「解压结果不是有效的数据库」，还把用户卡住，
    因为原逻辑不会回头下全量。
    """
    import apply_delta

    path = src
    tmp = None
    if src.endswith((".gz", ".zst")):
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".json", prefix="lawdelta-")
        os.close(fd)
        decompress(src, tmp)
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
    expect = art.get("sha256")
    if not expect:
        # 没有校验值就没法判断这个源给的是不是完整数据；宁可换源，也不装来路不明的包。
        raise SystemExit(f"manifest 里没有 {art['name']} 的 sha256，拒绝安装未校验的数据。")
    suffix = os.path.splitext(art["name"])[1]
    fd, tmp = tempfile.mkstemp(suffix=suffix, prefix="lawfetch-")
    os.close(fd)
    try:
        download(url, tmp, art["name"])
        got = _sha256(tmp)
        if got != expect:
            raise SystemExit(
                f"校验失败：期望 {expect[:16]}…，实际 {got[:16]}…\n文件可能损坏或被改动。")
        print("  校验通过")
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return tmp


def _run_source(label: str, manifest: dict, assets: dict, db: str,
                cur_ver: str | None, args) -> None:
    """从一个源完成安装：有增量且版本够近就走增量，失败自动回落全量。

    任何一步失败都抛 SystemExit，由 main() 接住换下一个源 —— 单个源坏掉
    （镜像挂了、文件被截断、sha256 对不上）不该让整次安装停在这里。
    """
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
            raise SystemExit(f"manifest 列了增量包 {art['name']}，但这个源里没有该文件。")
        tmp = None
        try:
            _step("下载增量包")
            tmp = _fetch_one(art, url)
            _step("应用增量")
            result = install_delta(tmp, db)
            print(f"\n已更新到 {result.get('to_version')}（来源：{label}）。")
            return
        except SystemExit as e:
            # 增量这条路任何一步不通（格式不认、本地版本太老、解压失败…）
            # 都退回全量，而不是把用户顶在这里。
            print(f"\n增量更新未成功（来源：{label}）：{e}")
            print("改成下载全量库，这一步会重新下 600 MB 以上，请稍等。")
            art = None
        finally:
            if tmp and not args.keep and os.path.exists(tmp):
                os.remove(tmp)

    if art is None:
        art = _pick_full(manifest, args.prefer_gz, have)

    url = assets.get(art["name"])
    if not url:
        raise SystemExit(f"manifest 列了 {art['name']}，但这个源里没有该文件。")

    os.makedirs(os.path.dirname(db) or ".", exist_ok=True)
    tmp = None
    try:
        _step(f"下载 {art['name']}")
        tmp = _fetch_one(art, url)   # 内含 sha256 校验，校验不过会抛错并清理临时文件
        _step("解压并安装")
        install_from_file(tmp, db, None)
    finally:
        if tmp and not args.keep and os.path.exists(tmp):
            os.remove(tmp)


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
    bases = mirror_bases()
    if not repo and not bases:
        raise SystemExit(
            "不知道该从哪个仓库下载。请指定：\n"
            "  python3 tools/fetch_data.py --repo owner/china-law-search\n"
            "或设置环境变量 LAW_REPO=owner/china-law-search。"
        )
    if repo:
        print(f"仓库：{repo}")
    if bases:
        print(f"镜像：{len(bases)} 个（按配置顺序尝试，GitHub 最后兜底）")

    # 收集所有可用源：镜像按用户给的顺序在前，GitHub 永远最后。
    sources: list[tuple[str, dict, dict]] = []
    for base in bases:
        src = load_mirror(base)
        if src:
            sources.append(src)
    if repo:
        src = load_github(repo)
        if src:
            sources.append(src)

    if not sources:
        tried = [f"镜像 {b}" for b in bases] + ([f"GitHub {repo}"] if repo else [])
        raise SystemExit(_all_sources_failed(tried))

    # 每个源标称的版本可能不同，取最高的那批先用；那一批全失败再退到次高版本。
    groups: dict[tuple, list] = {}
    for src in sources:
        key = version_key(src[1].get("corpus_version") or "")
        groups.setdefault(key, []).append(src)
    order = sorted(groups, reverse=True)

    for label, manifest, _ in sources:
        print(f"  {label}: {manifest.get('corpus_version', '?')}")

    best_label, best_manifest, _ = groups[order[0]][0]
    remote_ver = best_manifest.get("corpus_version", "")
    print(f"Release：{remote_ver}  "
          f"（法规 {best_manifest.get('law_count', '?')} 部 / "
          f"法条 {best_manifest.get('article_count', '?')} 条，来自 {best_label}）")

    cur_ver = local_version(db)
    if cur_ver:
        print(f"本地：{cur_ver}  →  {db}")
    else:
        print(f"本地：无可用库（{db}）")

    if cur_ver and version_key(cur_ver) >= version_key(remote_ver) and not args.full:
        print("\n已是最新，无需下载。要强制重下全量，加 --full。")
        return 0

    if args.check:
        print(f"\n有新版本可用，将从 {best_label} 下载。")
        return 0

    # 按版本从高到低、同版本按用户给的顺序逐个尝试；任一源失败自动换下一个。
    tried: list[str] = []
    for key in order:
        for label, manifest, assets in groups[key]:
            tried.append(label)
            try:
                _run_source(label, manifest, assets, db, cur_ver, args)
                return 0
            except SystemExit as e:
                print(f"\n源 {label} 失败：{e}")
                print("换下一个下载源重试…")
            except Exception as e:  # noqa: BLE001 - 单个源的意外错误不应中断整次安装
                print(f"\n源 {label} 出错：{e}")
                print("换下一个下载源重试…")

    raise SystemExit(_all_sources_failed(tried))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\n已取消，本次没有改动数据。", file=sys.stderr)
        sys.exit(130)
    except Exception as e:  # noqa: BLE001 —— 不能让用户对着栈自己猜
        print(f"\n安装未完成：{type(e).__name__}: {e}", file=sys.stderr)
        print(f"  卡在：{_STEP}", file=sys.stderr)
        print("  已有的库没有被覆盖。把这段报错贴给维护者即可："
              "https://github.com/tlgzs/china-law-search/issues", file=sys.stderr)
        sys.exit(1)
