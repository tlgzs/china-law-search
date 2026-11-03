# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""检查 GitHub 上有没有更新的数据包 / 程序版本，可选直接装。

两件事分开看，因为它们的更新方式完全不同：

  数据（法条库）  下载增量包 → 事务内入库 → 重建索引，可以全自动
  程序（本仓库）  改的是 Python 源码，只能 git pull + 重启，不能自动做

数据侧不重写下载逻辑，全部复用 tools/fetch_data.py —— 那里已经把
「挑 delta 还是全量、sha256 校验、增量失败回落全量」都写好了，
这里只负责判断和展示。两处各写一套，迟早出现「检查说有新版、
更新却装不上」的错位。

用法：
  python3 tools/check_update.py              # 只检查
  python3 tools/check_update.py --update     # 检查并安装数据更新
  python3 tools/check_update.py --json       # 结构化输出（给脚本/网页用）
  python3 tools/check_update.py --exit-code  # 有更新时退出码 10，方便 cron

环境变量：
  LAW_REPO       owner/name，默认从 git remote origin 推断
  GITHUB_TOKEN   可选，未认证时 API 限额 60 次/小时，够用；配了更稳
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import fetch_data as fd  # noqa: E402

TAGS_URL = "https://api.github.com/repos/{repo}/tags?per_page=100"
TAG_PREFIX = "app-v"

EXIT_HAS_UPDATE = 10


def _now() -> str:
    return datetime.datetime.now().astimezone().replace(microsecond=0).isoformat()


def _human(n: float | None) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _get_json(url: str, timeout: int = 15) -> object:
    """GET 一个 JSON。带 token 时自动加 Authorization（限额 60 → 5000/h）。"""
    headers = {
        "User-Agent": fd.USER_AGENT,
        "Accept": "application/vnd.github+json",
    }
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def local_app_version() -> str:
    """本地程序版本。与 server/law_search_service.py 读同一份 VERSION 文件。"""
    try:
        p = os.path.join(_ROOT, "VERSION")
        with open(p, encoding="utf-8") as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def remote_app_version(repo: str) -> tuple[str, str]:
    """远端最新的程序版本。返回 (版本号, tag 名)，取不到返回 ("", "")。

    程序版本用独立 tag（app-v2.4.0）而不是塞进数据 Release 的 manifest：
    manifest 只有发数据包时才有，程序改个错别字也要重发 600 MB 的数据包不合理。
    """
    try:
        tags = _get_json(TAGS_URL.format(repo=repo))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return "", ""
    if not isinstance(tags, list):
        return "", ""
    best_v, best_t = "", ""
    for t in tags:
        name = (t or {}).get("name") or ""
        if not name.startswith(TAG_PREFIX):
            continue
        ver = name[len(TAG_PREFIX):].lstrip("vV")
        if not re.fullmatch(r"\d+(\.\d+)*", ver):
            continue
        if not best_v or fd.version_key(ver) > fd.version_key(best_v):
            best_v, best_t = ver, name
    return best_v, best_t


def _use_direct_connection() -> None:
    """绕过 http_proxy / https_proxy 直连。

    代理失效时会返回 502/407 这类错误，用户看到的却是「连不上 GitHub」，
    很难想到是代理的问题。这里不改用户环境变量，只换 urllib 的全局 opener，
    进程内生效，不影响同一台机器上的其他程序。
    """
    urllib.request.install_opener(
        urllib.request.build_opener(urllib.request.ProxyHandler({})))


def _looks_like_proxy_error(msg: str) -> bool:
    m = (msg or "").lower()
    return any(k in m for k in ("502", "407", "tunnel", "proxy", "bad gateway"))


def check(repo: str, db: str, prefer_gz: bool = False) -> dict:
    """同时检查数据与程序。任何一侧失败都不影响另一侧，也不抛异常。

    返回结构直接给网页和脚本用，所以字段名要稳：
      ok      整体是否查通（网络/仓库问题时为 False，并带 error）
      data    数据侧 {local, remote, has_update, mode, ...}
      app     程序侧 {local, remote, has_update, tag, url}

    先按系统代理查一次；失败且看起来是代理问题时，绕开代理重试一次。
    企业内网常常只有代理能出网，所以不能一上来就直连。
    """
    first = _check_once(repo, db, prefer_gz)
    if first.get("ok") or not _looks_like_proxy_error(first.get("error", "")):
        return first
    _use_direct_connection()
    second = _check_once(repo, db, prefer_gz)
    if second.get("ok"):
        second.setdefault("warnings", []).append(
            "系统代理连不上 GitHub，已改为直连。若是公司网络必须走代理，请检查代理设置。")
    else:
        second["error"] = (second.get("error") or "") + "（代理与直连都失败）"
    return second


def _check_once(repo: str, db: str, prefer_gz: bool) -> dict:
    out: dict = {
        "ok": True,
        "repo": repo,
        "checked_at": _now(),
        "data": {"local": None, "remote": None, "has_update": False},
        "app": {"local": local_app_version(), "remote": "", "has_update": False},
        "warnings": [],
    }

    # ---------- 数据侧 ----------
    try:
        manifest, assets = fd.fetch_manifest(repo)
        remote_ver = manifest.get("corpus_version") or ""
        cur_ver = fd.local_version(db) or ""
        data = out["data"]
        data.update({
            "local": cur_ver or None,
            "remote": remote_ver or None,
            "law_count": manifest.get("law_count"),
            "article_count": manifest.get("article_count"),
            "schema_version": manifest.get("schema_version"),
            "release_title": manifest.get("corpus_version"),
        })
        if cur_ver and remote_ver:
            if fd.version_key(remote_ver) > fd.version_key(cur_ver):
                data["has_update"] = True
            elif fd.version_key(cur_ver) > fd.version_key(remote_ver):
                # 开发机自己跑库通常就是这种：本地已入库的数据比已发布的还新
                out["warnings"].append(
                    f"本地库 {cur_ver} 比线上发布版 {remote_ver} 更新（开发态），无需更新"
                )
        elif not cur_ver:
            data["has_update"] = True
            data["note"] = "本地没有库，首次安装需要全量"

        if data["has_update"] and cur_ver:
            delta = fd.pick_artifact(
                manifest.get("artifacts", []), "delta", prefer_gz, set(assets))
            min_v = (delta or {}).get("min_version")
            if delta and min_v and fd.version_key(cur_ver) >= fd.version_key(min_v):
                data["mode"] = "delta"
                data["size"] = delta.get("size")
                data["size_text"] = _human(delta.get("size"))
                data["artifact"] = delta.get("name")
                data["min_version"] = min_v
                data["scope"] = "delta"
                # 增量包自己也带 law_count，那是「这次变了多少部」，与 manifest
                # 顶层的全库数不是一回事。混用会让人以为下 1.4 MB 就能拿到全部
                # 一万六千多部法，实际只有变更过的几百部。
                data["law_count"] = delta.get("law_count")
                data["article_count"] = delta.get("article_count")
                data["repealed_count"] = delta.get("repealed_count") or 0
            else:
                data["mode"] = "full"
                data["scope"] = "full"
                # 直接用 pick_artifact 而不是 fetch_data._pick_full：
                # 后者会 print「下载全量库…」，检查阶段不该有下载动作的输出。
                full = fd.pick_artifact(
                    manifest.get("artifacts", []), "full", prefer_gz, set(assets))
                data["size"] = (full or {}).get("size")
                data["size_text"] = _human((full or {}).get("size"))
                data["artifact"] = (full or {}).get("name")
                if not delta:
                    data["why_full"] = "该 Release 未附带增量包"
                elif min_v:
                    data["why_full"] = f"本地版本早于增量包起点 {min_v}，需下全量"
    except SystemExit as e:  # fetch_manifest 用 SystemExit 报「仓库没有 Release」
        out["ok"] = False
        out["error"] = str(e).replace("\n", " ").strip()
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        out["ok"] = False
        out["error"] = f"连不上 GitHub：{type(e).__name__}: {e}"

    # ---------- 程序侧 ----------
    remote_v, tag = remote_app_version(repo)
    app = out["app"]
    if remote_v:
        app["remote"] = remote_v
        app["tag"] = tag
        app["url"] = f"https://github.com/{repo}/releases/tag/{tag}"
        if fd.version_key(remote_v) > fd.version_key(app["local"]):
            app["has_update"] = True
    else:
        app["note"] = "远端没有 app-v* 的 tag，程序版本无法比对"

    return out


def render(status: dict) -> str:
    """把 check() 的结果排成人看的文本。"""
    lines: list[str] = []
    if not status.get("ok"):
        lines.append(f"检查失败：{status.get('error')}")
        lines.append("（数据不受影响，可以继续用；稍后重试或检查网络。）")
        return "\n".join(lines)

    d = status["data"]
    lines.append("数据")
    if not d.get("remote"):
        lines.append("  线上没有可用的数据包")
    elif not d.get("local"):
        lines.append(f"  本地 无库  →  线上 {d['remote']}   首次安装，需下全量"
                     f"（{d.get('size_text', '?')}）")
    elif d.get("has_update"):
        how = "增量" if d.get("mode") == "delta" else "全量"
        lines.append(f"  本地 {d['local']}  →  线上 {d['remote']}   "
                     f"可{how}更新，约 {d.get('size_text', '?')}")
        if d.get("law_count"):
            if d.get("scope") == "delta":
                lines.append(f"       本次变更 {d['law_count']} 部法规 / "
                             f"{d.get('article_count') or '?'} 条正文")
                if d.get("repealed_count"):
                    lines.append(f"       其中 {d['repealed_count']} 部整部废止")
            else:
                # manifest 顶层的 law_count 只数「有现行版本」的法规（16874），
                # 另有几十部是已公布、尚未施行的新法：条文已经入库、也算进
                # article_count，但没有现行版本。写成「全库 N 部法规」会和条数
                # 自相矛盾，所以把两个数分开表述。
                lines.append(f"       全库 {d.get('article_count') or '?'} 条正文 / "
                             f"{d['law_count']} 部法规有现行版本")
        if d.get("why_full"):
            lines.append(f"       为何不走增量：{d['why_full']}")
        lines.append("       运行 python3 tools/check_update.py --update 安装")
    else:
        # has_update=False 有两种可能：真的对齐，或本地比线上还新（开发机常见）。
        # 两种情况都写「已是最新」会让人误以为线上已经包含本地这些数据。
        if d.get("local") and fd.version_key(d["local"]) > fd.version_key(d["remote"]):
            lines.append(f"  本地 {d['local']}  比线上发布版 {d['remote']} 新（开发态，无需更新）")
        else:
            lines.append(f"  本地 {d['local']}  已是最新（线上 {d['remote']}）")

    a = status["app"]
    lines.append("程序")
    if a.get("has_update"):
        lines.append(f"  本地 {a['local']}  →  线上 {a['remote']}   有新版本")
        lines.append(f"       git pull 后重启服务；说明见 {a.get('url', '')}")
    elif a.get("remote"):
        lines.append(f"  本地 {a['local']}  已是最新（线上 {a['remote']}）")
    else:
        lines.append(f"  本地 {a['local']}  {a.get('note', '')}")

    for w in status.get("warnings") or []:
        lines.append(f"注意：{w}")
    return "\n".join(lines)


def do_update(repo: str, db: str, prefer_gz: bool = False, keep: bool = False) -> dict:
    """下载并安装数据更新。只动数据，程序部分仅提示。"""
    status = check(repo, db, prefer_gz)
    if not status.get("ok"):
        return {"ok": False, "error": status.get("error")}
    d = status["data"]
    if not d.get("has_update"):
        return {"ok": True, "already_latest": True, "to_version": d.get("local")}

    # 复用 fetch_data 的主流程：挑资产 → 下载 → 校验 sha256 → 装 → 失败回落全量
    argv = ["--repo", repo, "--db", db]
    if prefer_gz:
        argv.append("--prefer-gz")
    if keep:
        argv.append("--keep")
    old_argv = sys.argv
    try:
        sys.argv = ["fetch_data.py", *argv]
        code = fd.main()
    except SystemExit as e:
        # fetch_data 用 SystemExit 报「资产缺失 / 校验失败」这类致命错误。
        # 在 CLI 里直接退掉会丢掉「程序侧还有新版本」这条提示，所以收成返回值。
        return {"ok": False, "error": str(e).replace("\n", " ").strip()}
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return {"ok": False, "error": f"下载失败：{type(e).__name__}: {e}"}
    finally:
        sys.argv = old_argv
    return {"ok": code == 0, "to_version": d.get("remote")}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="检查并更新法条库 / 程序版本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("用法：")[1] if "用法：" in __doc__ else None,
    )
    ap.add_argument("--repo", default=None, help="GitHub 仓库 owner/name")
    ap.add_argument("--db", default=os.path.join(_ROOT, "data", "law_data.db"))
    ap.add_argument("--update", action="store_true", help="发现有数据更新就装上")
    ap.add_argument("--json", action="store_true", help="输出 JSON，不排文本")
    ap.add_argument("--exit-code", action="store_true",
                    help=f"有任何一侧可更新时退出码 {EXIT_HAS_UPDATE}")
    ap.add_argument("--prefer-gz", action="store_true", help="优先 .gz 包")
    ap.add_argument("--keep", action="store_true", help="保留下载的压缩包")
    args = ap.parse_args()

    repo = args.repo or fd.guess_repo()
    if not repo:
        msg = ("不知道该查哪个仓库。指定 --repo owner/name，"
               "或设置环境变量 LAW_REPO=owner/name。")
        if args.json:
            print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
        else:
            print(msg)
        return 2

    db = os.path.abspath(os.path.expanduser(args.db))

    if args.update:
        result = do_update(repo, db, args.prefer_gz, args.keep)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=1))
        elif result.get("already_latest"):
            print("数据已是最新，无需更新。")
        elif result.get("ok"):
            print(f"\n数据已更新到 {result.get('to_version')}。")
        return 0 if result.get("ok") else 1

    status = check(repo, db, args.prefer_gz)
    if args.json:
        print(json.dumps(status, ensure_ascii=False, indent=1))
    else:
        print(render(status))
    if args.exit_code and status.get("ok"):
        if status["data"].get("has_update") or status["app"].get("has_update"):
            return EXIT_HAS_UPDATE
    return 0 if status.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
