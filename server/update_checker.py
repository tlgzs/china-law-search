# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""进程内的「检查更新」缓存层，供 HTTP 接口与启动钩子共用。

为什么要有这一层，而不是接口里直接调 tools/check_update.py：

  启动时查一次、网页再点一次、用户刷新又查一次，每次都打 GitHub 会被限流
  （未认证 60 次/小时）。这里把结果缓存 TTL 秒，任何人来问都先读缓存。

真正的判断逻辑只有一份，在 tools/check_update.py。这一层只做缓存、加锁、
线程化 —— 两边各写一套判断迟早出现「网页说有新版、命令行说没有」。

更新是写库操作，用独立的锁串行化：网页上连点两下不会并发跑两个增量包。
"""
from __future__ import annotations

import os
import sys
import threading
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOLS = os.path.join(_ROOT, "tools")
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import check_update as cu  # noqa: E402

# 缓存有效秒数。LAW_UPDATE_CHECK_TTL=0 表示每次都真查。
TTL = int(os.environ.get("LAW_UPDATE_CHECK_TTL", "3600"))
# 启动时查到有新版就自动装。默认关：改本地库这件事不该悄悄发生。
AUTO_UPDATE = os.environ.get("LAW_AUTO_UPDATE", "0") not in ("0", "", "false", "no")

_cache: dict = {"at": 0.0, "status": None}
_cache_lock = threading.Lock()
_update_lock = threading.Lock()
_last_update: dict = {"at": None, "result": None}


def _default_db() -> str:
    return os.environ.get("LAW_DB_PATH",
                          os.path.join(_ROOT, "data", "law_data.db"))


def _repo() -> str:
    return os.environ.get("LAW_REPO") or cu.fd.guess_repo()


def get_status(force: bool = False, db: str | None = None) -> dict:
    """查数据与程序版本。带 TTL 缓存；force=True 强制联网。"""
    repo = _repo()
    if not repo:
        return {"ok": False, "error": "未配置仓库：设置 LAW_REPO=owner/name"}

    now = time.time()
    with _cache_lock:
        fresh = _cache["status"] is not None and (now - _cache["at"]) < TTL
        if fresh and not force:
            status = dict(_cache["status"])
            status["cached"] = True
            status["cache_age"] = int(now - _cache["at"])
            return status
        status = cu.check(repo, db or _default_db())
        _cache["at"] = now
        _cache["status"] = status

    out = dict(status)
    out["cached"] = False
    out["cache_age"] = 0
    out["ttl"] = TTL
    return out


def run_update(db: str | None = None) -> dict:
    """执行数据更新。同一时刻只允许一个。"""
    if not _update_lock.acquire(blocking=False):
        return {"ok": False, "busy": True, "error": "已有更新在进行中，请稍候"}
    try:
        repo = _repo()
        if not repo:
            return {"ok": False, "error": "未配置仓库：设置 LAW_REPO=owner/name"}
        result = cu.do_update(repo, db or _default_db())
        _last_update["at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        _last_update["result"] = result
        # 更新完缓存必失效：版本号变了，旧结论立刻过期
        with _cache_lock:
            _cache["at"] = 0.0
            _cache["status"] = None
        return result
    except Exception as e:  # noqa: BLE001 —— 接口层不能把栈抛给浏览器
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        _update_lock.release()


def last_update() -> dict:
    return dict(_last_update)


def warm_cache_async(db: str | None = None) -> None:
    """启动时在后台查一次，让网页打开就有结论，又不拖慢启动。

    断网时不重试、不打日志噪音：老库照常能用，等用户自己点「检查更新」。
    """
    def _work() -> None:
        try:
            status = get_status(db=db)
            if AUTO_UPDATE and status.get("ok") and status["data"].get("has_update"):
                run_update(db=db)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_work, name="update-check", daemon=True).start()


def is_local_host(host: str | None) -> bool:
    """只允许本机调用更新接口。

    更新会改用户本地库，不能因为网页在公网上开着就让任何人都能触发。
    经反向代理转发的请求，client.host 是代理地址，会被这里挡住 —— 这正是预期。
    """
    h = (host or "").strip("[]")
    return h in ("127.0.0.1", "::1", "localhost", "0:0:0:0:0:0:0:1")
