# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""条文状态推导 —— 全系统唯一实现。

三个入口都调这里，避免各写一套判定：
  - LawStore.verify（HTTP /api/law/verify 与 MCP verify_citation）
  - LawStore.get_article（HTTP /api/law/article 与 MCP get_article）

判定全部从「版本 + 条文」两表本身算出来，不依赖人工标注的新字段：
  law_versions.status            版本是否现行 / 已公布未生效 / 被取代
  law_articles.status            条文本版本内是否被废止
  law_articles.repeal_reason     被新修订取代，还是被废止决定废止
  law_versions.superseded_by     被哪一版顶掉（可指向废止决定）

法规层（law_status）：
  effective           存在现行版或已公布未生效版
  repealed            已无现行版，且末版被废止决定终止
  no_current_version  已无现行版，但末版是被新修订取代（疑似未收录最新版）

条文层（article_status）：
  in_force            现行版存在该条号
  removed_in_current  现行版无、旧版有 → 在现行版公布日对应的修订中删除或合并
  repealed            该条本身被废止（整部法废止，或现行版内单独标废止）
  renumbered          现行版无该条号，但能匹配到内容高度相近的新条号
  unknown             库内没有现行版，无法判断该条是否仍有效

兼容口径：
  status 字段保留，但值改准：
    in_force → effective / pending（沿用旧值）
    removed_in_current → removed（旧实现误报为 repealed，这是本次修复的核心）
    repealed → repealed
    renumbered → renumbered
    unknown → unknown
  is_repealed 仍表示「这条引文已失效」，只在 repealed 时为 true。
"""

from __future__ import annotations

import difflib
import sqlite3

# 法规层
LAW_EFFECTIVE = "effective"
LAW_REPEALED = "repealed"
LAW_NO_CURRENT = "no_current_version"

# 条文层
ARTICLE_IN_FORCE = "in_force"
ARTICLE_REMOVED = "removed_in_current"
ARTICLE_REPEALED = "repealed"
ARTICLE_RENUMBERED = "renumbered"
ARTICLE_UNKNOWN = "unknown"

# 版本层
VERSION_EFFECTIVE = "effective"
VERSION_PENDING = "pending"

# 最高人民法院的「解释」类文件会收录被解释法的同名条文，定位时必须让开
_INTERPRETATION_MARK = "解释"

# 重新编号只认「内容高度相近」，不做模糊猜测；阈值写死，避免随语料漂移
_RENUMBER_THRESHOLD = 0.90
_RENUMBER_MAX_CANDIDATES = 3

_ARTICLE_COLS = (
    "law_name, article_number, article_number_num, article_suffix, chapter, section, "
    "article, content, publish_date, effective_date, status, repeal_date, repeal_reason, "
    "replaced_by, is_principle_clause, source_file, version_id"
)


# ============================================================
# 基础工具
# ============================================================

def normalize_law_name(name: str) -> str:
    """去书名号与首尾空白，与 law_query_core 的口径保持一致。"""
    return (name or "").strip().replace("《", "").replace("》", "")


def _as_names(names) -> list[str]:
    """把单个法名或候选法名列表摊平，并各自补一个归一化名。"""
    if isinstance(names, str):
        names = [names]
    out: list[str] = []
    for raw in names or []:
        n = (raw or "").strip()
        if not n:
            continue
        for v in (n, normalize_law_name(n)):
            if v and v not in out:
                out.append(v)
    return out


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _suffix_clause(article_suffix: str) -> tuple[str, tuple]:
    """没指定后缀时只认正条，避免「第八十二条」把「第八十二条之一」串进来。"""
    if article_suffix:
        return " AND article_suffix = ?", (article_suffix,)
    return " AND (article_suffix = '' OR article_suffix IS NULL)", ()


def _as_dict(row) -> dict:
    """sqlite3.Row / tuple 都转成 dict，方便内部传递。"""
    if isinstance(row, dict):
        return row
    if hasattr(row, "keys"):
        return {k: row[k] for k in row.keys()}
    return dict(row)


# ============================================================
# 法规层：版本与整体状态
# ============================================================

def _fetch_version_rows(conn: sqlite3.Connection, names: list[str]) -> tuple[str | None, list]:
    """按候选法名取版本行。返回（库内规范名，按时间升序的版本行）。"""
    if not _has_table(conn, "law_versions"):
        return None, []
    for name in names:
        rows = conn.execute(
            """SELECT id, law_name, norm_name, publish_date, effective_date, status,
                      article_count, superseded_by
               FROM law_versions
               WHERE law_name = ? OR norm_name = ?
               ORDER BY COALESCE(effective_date, publish_date, '') ASC, id ASC""",
            (name, name),
        ).fetchall()
        if rows:
            return rows[0]["law_name"], rows
    return None, []


def _version_article_stats(conn: sqlite3.Connection, version_id: int) -> dict:
    """一个版本的条文统计：总数、仍有效数、废止原因分布。

    「被新修订版本取代」与「被…废止」是两种完全不同的语义，必须分开数：
    前者说明这部法换了一版继续用，后者才是真的不要了。
    """
    row = conn.execute(
        """SELECT COUNT(*) AS n,
                  COALESCE(SUM(CASE WHEN status IS NULL OR status != 'repealed'
                                    THEN 1 ELSE 0 END), 0) AS live,
                  COALESCE(SUM(CASE WHEN repeal_reason LIKE '%废止%'
                                    THEN 1 ELSE 0 END), 0) AS repealed_by_decision,
                  COALESCE(SUM(CASE WHEN repeal_reason LIKE '%取代%'
                                    THEN 1 ELSE 0 END), 0) AS superseded_by_revision
           FROM law_articles WHERE version_id = ?""",
        (version_id,),
    ).fetchone()
    return {
        "article_count": row["n"] or 0,
        "live_count": row["live"] or 0,
        "repealed_by_decision": row["repealed_by_decision"] or 0,
        "superseded_by_revision": row["superseded_by_revision"] or 0,
    }


def _version_is_repealed(conn: sqlite3.Connection, version: dict, stats: dict) -> bool:
    """该版本是否「因废止而终止」。

    版本显式标 repealed 直接算；否则看条文：全部标 repealed、废止原因里出现
    「废止」、且没有任何「被新修订版本取代」—— 三条同时成立才算。
    只靠「条文全 repealed」会误伤被新版顶掉的旧版（库里旧版条文也是 repealed）。
    """
    if (version.get("status") or "") == "repealed":
        return True
    return (
        stats["article_count"] > 0
        and stats["live_count"] == 0
        and stats["repealed_by_decision"] > 0
        and stats["superseded_by_revision"] == 0
    )


def _repeal_info(conn: sqlite3.Connection, version_id: int) -> tuple[str, str]:
    """从条文上取废止日期与依据（版本表没有这两列）。"""
    row = conn.execute(
        """SELECT repeal_date, repeal_reason FROM law_articles
           WHERE version_id = ? AND repeal_reason LIKE '%废止%'
           ORDER BY id DESC LIMIT 1""",
        (version_id,),
    ).fetchone()
    if not row:
        return "", ""
    return row["repeal_date"] or "", row["repeal_reason"] or ""


def _version_dict(conn: sqlite3.Connection, row, current_id: int | None) -> dict:
    stats = _version_article_stats(conn, row["id"])
    return {
        "version_id": row["id"],
        "publish_date": row["publish_date"] or "",
        "effective_date": row["effective_date"] or "",
        "status": row["status"] or VERSION_EFFECTIVE,
        "is_current": row["id"] == current_id,
        # 用实际条文数，不信元数据：元数据在增量入库里可能没更新
        "article_count": stats["article_count"],
    }


def _articles_only_fallback(conn: sqlite3.Connection, names: list[str]) -> dict | None:
    """老库没有 law_versions 时的降级：只能按条文状态粗判。"""
    for name in names:
        row = conn.execute(
            """SELECT COUNT(*) AS n,
                      COALESCE(SUM(CASE WHEN status IS NULL OR status != 'repealed'
                                        THEN 1 ELSE 0 END), 0) AS live
               FROM law_articles WHERE law_name = ?""",
            (name,),
        ).fetchone()
        if not row or not row["n"]:
            continue
        return {
            "law_name": name,
            "law_status": LAW_EFFECTIVE if row["live"] else LAW_REPEALED,
            "versions": [],
            "current_version": None,
            "latest_version": None,
            "repeal_date": "",
            "repeal_reason": "",
            "has_multiple_versions": False,
            "version_source": "law_articles",
        }
    return None


def classify_law(conn: sqlite3.Connection, names) -> dict | None:
    """解析法规整体状态。names 为单个法名或候选法名列表（按优先级）。

    返回 None 表示库里完全没有这部法（版本表、条文表都没有）。
    """
    names = _as_names(names)
    canonical, rows = _fetch_version_rows(conn, names)
    if not rows:
        return _articles_only_fallback(conn, names)

    vrows = [{
        "id": r["id"],
        "law_name": r["law_name"],
        "publish_date": r["publish_date"] or "",
        "effective_date": r["effective_date"] or "",
        "status": r["status"] or VERSION_EFFECTIVE,
        "superseded_by": r["superseded_by"],
    } for r in rows]

    effective = [v for v in vrows if v["status"] == VERSION_EFFECTIVE]
    pending = [v for v in vrows if v["status"] == VERSION_PENDING]

    # 现行版优先取已生效版：新版 pending 时旧版仍是律师今天该引的文本
    current = (effective or pending or [None])[-1]
    current_id = current["id"] if current else None

    versions = [_version_dict(conn, r, current_id) for r in rows]
    latest = vrows[-1]
    latest_stats = _version_article_stats(conn, latest["id"])

    if current:
        law_status = LAW_EFFECTIVE
    elif _version_is_repealed(conn, latest, latest_stats):
        law_status = LAW_REPEALED
    else:
        law_status = LAW_NO_CURRENT

    repeal_date, repeal_reason = _repeal_info(conn, latest["id"])
    return {
        "law_name": canonical or latest["law_name"],
        "law_status": law_status,
        "versions": versions,
        "current_version": next((v for v in versions if v["is_current"]), None),
        "latest_version": versions[-1],
        "repeal_date": repeal_date,
        "repeal_reason": repeal_reason,
        "has_multiple_versions": len(versions) > 1,
        "version_source": "law_versions",
    }


# ============================================================
# 条文层：在某个版本里取条文
# ============================================================

def _article_in_version(conn: sqlite3.Connection, version_id: int,
                        article_num: int, article_suffix: str):
    clause, args = _suffix_clause(article_suffix)
    return conn.execute(
        f"""SELECT {_ARTICLE_COLS} FROM law_articles
            WHERE version_id = ? AND article_number_num = ?{clause}
            ORDER BY id DESC LIMIT 1""",
        (version_id, article_num, *args),
    ).fetchone()


def _latest_version_with_article(conn: sqlite3.Connection, versions: list[dict],
                                 article_num: int, article_suffix: str):
    """在给定版本（时间升序）里找最后一个含该条号的版本。"""
    for version in reversed(versions):
        row = _article_in_version(conn, version["version_id"], article_num, article_suffix)
        if row:
            return version, row
    return None, None


def _renumber_candidates(conn: sqlite3.Connection, version_id: int,
                         old_content: str) -> list[dict]:
    """在现行版里找内容高度相近的条文，作为重新编号候选。

    只做文本相似度，不做语义猜测：相似度低于阈值的宁可不报，
    报一个错的「新条号」比报「已删除」更容易误导律师。
    """
    old = (old_content or "").strip()
    if not old:
        return []
    rows = conn.execute(
        """SELECT article_number, content FROM law_articles
           WHERE version_id = ? AND (status IS NULL OR status != 'repealed')""",
        (version_id,),
    ).fetchall()
    scored: list[tuple[float, str]] = []
    old_len = len(old)
    for row in rows:
        new = (row["content"] or "").strip()
        if not new:
            continue
        # 长度差超过 20% 的直接排除，避免对大部头法规逐条精算
        if old_len and not (0.8 <= len(new) / old_len <= 1.2):
            continue
        ratio = difflib.SequenceMatcher(None, old, new).ratio()
        if ratio >= _RENUMBER_THRESHOLD:
            scored.append((ratio, row["article_number"]))
    scored.sort(reverse=True)
    return [
        {"article_number": num, "similarity": round(ratio, 4)}
        for ratio, num in scored[:_RENUMBER_MAX_CANDIDATES]
    ]


# ============================================================
# 结果组装
# ============================================================

def _empty_result(law: dict, article_num: int, article_suffix: str,
                  article_number_text: str = "") -> dict:
    return {
        "law_name": law["law_name"],
        "article_number": article_number_text or f"第{article_num}条{article_suffix}",
        "article_num": article_num,
        "article_suffix": article_suffix,
        "law_status": law["law_status"],
        "article_status": None,
        "citation_valid": False,
        "version": None,
        "content": None,
        "article": None,
        "chapter": None,
        "section": None,
        "publish_date": "",
        "effective_date": "",
        # 旧字段：保留，值按新语义校准
        "status": None,
        "is_pending": False,
        "is_repealed": False,
        "repeal_date": law.get("repeal_date", ""),
        "repeal_reason": law.get("repeal_reason", ""),
        "replaced_by": "",
        "is_principle_clause": False,
        "has_multiple_versions": law.get("has_multiple_versions", False),
        # 追溯旧版：被删 / 改号 / 整部废止时都要能拿到原文
        "previous_version": None,
        "previous_content": None,
        "previous_article_number": None,
        "renumber_candidates": [],
        "error": None,
        "message": None,
    }


def _fill_in_force(result: dict, law: dict, version: dict, row) -> dict:
    pending = version["status"] == VERSION_PENDING
    result.update({
        # 以行内条号为准：带「之一」等后缀时，调用方拼出来的写法可能不完整
        "article_number": row["article_number"] or result["article_number"],
        "article_status": ARTICLE_IN_FORCE,
        "citation_valid": not pending,
        "version": version,
        "content": row["content"],
        "article": row["article"],
        "chapter": row["chapter"],
        "section": row["section"],
        "publish_date": row["publish_date"] or version["publish_date"],
        "effective_date": row["effective_date"] or version["effective_date"],
        "status": VERSION_PENDING if pending else VERSION_EFFECTIVE,
        "is_pending": pending,
        "is_repealed": False,
        "repeal_date": row["repeal_date"] or "",
        "repeal_reason": row["repeal_reason"] or "",
        "replaced_by": row["replaced_by"] or "",
        "is_principle_clause": bool(row["is_principle_clause"]),
        "message": (
            f"该条文已公布但尚未生效，{version['effective_date']} 起施行，"
            "不得作为现行有效依据引用"
            if pending else None
        ),
    })
    return result


def _version_date_text(version: dict) -> str:
    """版本时间描述。缺施行日时不要吐「日期未知 施行」这种半截话。"""
    publish = version.get("publish_date") or ""
    effective = version.get("effective_date") or ""
    if publish and effective:
        return f"{publish} 公布、{effective} 施行"
    if publish:
        return f"{publish} 公布"
    if effective:
        return f"{effective} 施行"
    return "日期未知"


def _fill_removed(result: dict, current: dict, prev_version: dict, prev_row,
                  include_history: bool) -> dict:
    result.update({
        "article_status": ARTICLE_REMOVED,
        "citation_valid": False,
        "version": current,
        # 条号以旧版正文的写法为准：调用方可能只传了「第27条」这样的阿拉伯数字
        "article_number": prev_row["article_number"] or result["article_number"],
        "content": prev_row["content"] if include_history else None,
        "article": prev_row["article"] if include_history else None,
        "chapter": prev_row["chapter"] if include_history else None,
        "section": prev_row["section"] if include_history else None,
        "publish_date": current["publish_date"],
        "effective_date": current["effective_date"],
        "status": "removed",
        "is_pending": current["status"] == VERSION_PENDING,
        "is_repealed": False,
        "repeal_date": "",
        "repeal_reason": "",
        "replaced_by": "",
        "is_principle_clause": bool(prev_row["is_principle_clause"]),
        "previous_version": prev_version,
        "previous_content": prev_row["content"],
        "previous_article_number": prev_row["article_number"],
        "message": (
            f"现行版（{_version_date_text(current)}）中已无本条；"
            f"最后见于 {prev_version['publish_date'] or '日期未知'} 版，"
            "应为该次修订中删除或合并，原文见 previous_content。"
        ),
    })
    return result


def _fill_renumbered(result: dict, current: dict, prev_version: dict, prev_row,
                     candidates: list[dict]) -> dict:
    nums = "、".join(c["article_number"] for c in candidates)
    result.update({
        "article_status": ARTICLE_RENUMBERED,
        "citation_valid": False,
        "version": current,
        "publish_date": current["publish_date"],
        "effective_date": current["effective_date"],
        "status": "renumbered",
        "is_pending": current["status"] == VERSION_PENDING,
        "is_repealed": False,
        "repeal_date": "",
        "repeal_reason": "",
        "replaced_by": "",
        "is_principle_clause": bool(prev_row["is_principle_clause"]),
        "previous_version": prev_version,
        "previous_content": prev_row["content"],
        "previous_article_number": prev_row["article_number"],
        "renumber_candidates": candidates,
        "message": (
            f"现行版中已无本条号，但内容与 {nums} 高度相近，"
            f"可能是 {current['publish_date'] or '本次修订'} 时重新编号，请确认后再引用。"
        ),
    })
    return result


def _fill_repealed(result: dict, law: dict, version: dict, row,
                   article_only: bool) -> dict:
    repeal_date = row["repeal_date"] or law.get("repeal_date", "")
    repeal_reason = row["repeal_reason"] or law.get("repeal_reason", "")
    if article_only:
        message = "该条文已被单独废止，不得作为现行有效依据引用。"
    else:
        message = (
            f"该法规已于 {repeal_date} 废止" if repeal_date else "该法规已废止"
        )
        if repeal_reason:
            message += f"，依据：{repeal_reason}"
        message += "，不得作为现行有效依据引用。"
    result.update({
        "article_status": ARTICLE_REPEALED,
        "citation_valid": False,
        "version": version,
        # 整部法废止时仍返回原文，供追溯；引用与否由 citation_valid 决定
        "content": row["content"],
        "article": row["article"],
        "chapter": row["chapter"],
        "section": row["section"],
        "publish_date": row["publish_date"] or version["publish_date"],
        "effective_date": row["effective_date"] or version["effective_date"],
        "status": "repealed",
        "is_pending": False,
        "is_repealed": True,
        "repeal_date": repeal_date,
        "repeal_reason": repeal_reason,
        "replaced_by": row["replaced_by"] or "",
        "is_principle_clause": bool(row["is_principle_clause"]),
        "previous_version": version,
        "previous_content": row["content"],
        "previous_article_number": row["article_number"],
        "message": message,
    })
    return result


def _fill_unknown(result: dict, version: dict, row) -> dict:
    result.update({
        "article_status": ARTICLE_UNKNOWN,
        "citation_valid": False,
        "version": version,
        "content": row["content"],
        "article": row["article"],
        "chapter": row["chapter"],
        "section": row["section"],
        "publish_date": row["publish_date"] or version["publish_date"],
        "effective_date": row["effective_date"] or version["effective_date"],
        "status": "unknown",
        "is_pending": False,
        "is_repealed": False,
        "repeal_date": "",
        "repeal_reason": "",
        "replaced_by": "",
        "is_principle_clause": bool(row["is_principle_clause"]),
        "previous_version": version,
        "previous_content": row["content"],
        "previous_article_number": row["article_number"],
        "message": (
            f"库内仅有旧版（{version['publish_date'] or '日期未知'} 公布），"
            "未收录现行版，无法确认该条是否仍有效；建议核对源站或等待数据更新。"
        ),
    })
    return result


# ============================================================
# 统一入口
# ============================================================

def _row_is_repealed(row) -> bool:
    """行状态是否表示「真废止」。

    旧数据把「被下一版取代」的条文也写成 repealed，所以不能只看 status：
    必须同时有写明「废止」的原因，才算这条真的被废止决定终止。
    """
    if (row["status"] or "") != "repealed":
        return False
    return "废止" in (row["repeal_reason"] or "")


def _resolve_as_of(conn: sqlite3.Connection, law: dict, article_num: int,
                   article_suffix: str, day: str,
                   article_number_text: str = "") -> dict:
    """按时间点回答「当时是哪一版、这条当时有效吗」。

    办旧案子要引当年施行的文本，因此这里既不能拿现行版套，也不能把
    历史版本行上的 repealed 当成废止——那是被新版取代留下的脏标记。
    """
    result = _empty_result(law, article_num, article_suffix, article_number_text)
    result["as_of"] = day

    versions = law.get("versions") or []
    covered = [v for v in versions
               if v["effective_date"] and v["effective_date"] <= day]
    if not covered:
        result["error"] = "no_version_in_force"
        result["message"] = (
            f"该法规在 {day} 尚无施行版本（最早版本施行于 "
            f"{versions[0]['effective_date'] or '未知日期'}）。"
            if versions else f"该法规在 {day} 尚无施行版本。"
        )
        return result

    chosen = covered[-1]
    row = _article_in_version(conn, chosen["version_id"], article_num, article_suffix)
    if not row:
        result["error"] = "article_not_in_version"
        result["message"] = (
            f"该版本（施行于 {chosen['effective_date'] or '未知日期'}）没有 "
            f"第 {article_num} 条{article_suffix}。"
        )
        return result

    if _row_is_repealed(row):
        return _fill_repealed(result, law, chosen, row, article_only=False)

    filled = _fill_in_force(result, law, chosen, row)
    current = law.get("current_version") or {}
    is_current = chosen["version_id"] == current.get("version_id")
    if is_current:
        return filled

    # 历史版本：当时有效，但今天不能直接当现行依据引
    nxt = next((v for v in versions
                if (v["effective_date"] or "") > (chosen["effective_date"] or "")), None)
    filled.update({
        "citation_valid": False,
        "status": "superseded",
        "historical": True,
        "superseded_date": nxt["effective_date"] if nxt else "",
    })

    message = f"该条在 {day} 时有效（{_version_date_text(chosen)}）"
    if nxt:
        message += (
            f"；该版本自 {nxt['effective_date'] or '日期未知'} 起被"
            f" {nxt['publish_date'] or '新版'} 版取代"
        )
    if current and current.get("version_id") != chosen["version_id"]:
        if _article_in_version(conn, current["version_id"], article_num, article_suffix):
            message += "；现行版仍保留本条，引用请以现行版为准。"
        else:
            message += "；现行版中已无本条，本条最后见于该历史版本。"
    filled["message"] = message.rstrip("。") + "。"
    return filled


def resolve_article(conn: sqlite3.Connection, names, article_num: int,
                    article_suffix: str = "", include_history: bool = False,
                    article_number_text: str = "", as_of: str = "") -> dict | None:
    """法名 + 条号 → 唯一状态判定结果。返回 None 表示库里没有这部法。

    调用方拿到的是同一份结构，不需要再组合 status 判断是否可引用：
    看 citation_valid 就够了。传 `as_of` 时按该日期适用的版本回答。
    """
    law = classify_law(conn, names)
    if not law:
        return None

    if as_of:
        return _resolve_as_of(conn, law, article_num, article_suffix, as_of,
                              article_number_text)

    result = _empty_result(law, article_num, article_suffix, article_number_text)

    # ---- 老库降级：没有版本表，只能按条文现状给结论 ----
    if law["version_source"] == "law_articles":
        clause, args = _suffix_clause(article_suffix)
        row = None
        for name in _as_names(names):
            row = conn.execute(
                f"""SELECT {_ARTICLE_COLS} FROM law_articles
                    WHERE law_name = ? AND article_number_num = ?{clause}
                    ORDER BY id DESC LIMIT 1""",
                (name, article_num, *args),
            ).fetchone()
            if row:
                break
        if not row:
            result["error"] = "not_found"
            result["message"] = "未找到该条文。"
            return result
        version = {"version_id": row["version_id"], "publish_date": row["publish_date"] or "",
                   "effective_date": row["effective_date"] or "",
                   "status": row["status"] or VERSION_EFFECTIVE,
                   "is_current": True, "article_count": 0}
        if (row["status"] or "") == "repealed":
            return _fill_repealed(result, law, version, row, article_only=True)
        return _fill_in_force(result, law, version, row)

    current = law["current_version"]

    # ---- 有现行版 ----
    if law["law_status"] == LAW_EFFECTIVE and current:
        row = _article_in_version(conn, current["version_id"], article_num, article_suffix)
        if row and (row["status"] or "") != "repealed":
            return _fill_in_force(result, law, current, row)
        if row:
            # 现行版内该条被单独废止
            return _fill_repealed(result, law, current, row, article_only=True)

        # 现行版没有 → 先看尚未生效的新版（老实现也是这么取到 pending 条文的）
        if current["status"] == VERSION_EFFECTIVE:
            newer_pending = [
                v for v in law["versions"]
                if v["status"] == VERSION_PENDING and v["version_id"] != current["version_id"]
            ]
            if newer_pending:
                pending_version = newer_pending[-1]
                pending_row = _article_in_version(
                    conn, pending_version["version_id"], article_num, article_suffix)
                if pending_row and (pending_row["status"] or "") != "repealed":
                    return _fill_in_force(result, law, pending_version, pending_row)

        # 在现行版之前的版本里找
        older = [v for v in law["versions"]
                 if v["version_id"] != current["version_id"]
                 and (v["publish_date"] or "") <= (current["publish_date"] or "9999-99-99")]
        prev_version, prev_row = _latest_version_with_article(
            conn, older, article_num, article_suffix)
        if prev_row:
            candidates = _renumber_candidates(
                conn, current["version_id"], prev_row["content"])
            if candidates:
                return _fill_renumbered(result, current, prev_version, prev_row, candidates)
            return _fill_removed(result, current, prev_version, prev_row, include_history)

        result["error"] = "not_found"
        result["message"] = "该法规现行版及历史版本均无此条号。"
        return result

    # ---- 整部法已废止 ----
    if law["law_status"] == LAW_REPEALED:
        version, row = _latest_version_with_article(
            conn, law["versions"], article_num, article_suffix)
        if row:
            return _fill_repealed(result, law, version, row, article_only=False)
        result["error"] = "not_found"
        result["message"] = "该法规已废止，且历史版本中无此条号。"
        return result

    # ---- 无现行版本 ----
    version, row = _latest_version_with_article(
        conn, law["versions"], article_num, article_suffix)
    if row:
        return _fill_unknown(result, version, row)
    result["error"] = "not_found"
    result["message"] = "该法规未收录现行版，历史版本中也无此条号。"
    return result


def version_summary(law: dict) -> dict:
    """给上层做展示用的精简版本摘要（不含全部版本）。"""
    version = law.get("current_version") or law.get("latest_version") or {}
    return {
        "version_id": version.get("version_id"),
        "publish_date": version.get("publish_date", ""),
        "effective_date": version.get("effective_date", ""),
        "is_current": bool(version.get("is_current")),
        "article_count": version.get("article_count", 0),
    }
