# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""修复「新版还没生效就把旧版作废」的存量数据。

背景：早期入库见「不是新增」就把同名的旧现行版降级成 superseded、旧条文全标
repealed，没有检查新版的施行日。结果是《农业法》《商标法》这类「新版明年才
施行」的法，在库里一条可用正文都不剩 —— 检索只能拿到明年才生效的文本，而
今天该引的旧版查不到。

现在的代码已经不再这么做了（tools/update.py:supersede_old 加了 pending 判断，
ingest/schema.py:refresh_pending_status 补了「生效当天才降级旧版」的级联），
但已经写坏的库需要这道一次性修复。

判定：某部法存在 pending 版本、却**没有任何 effective 版本**时，说明旧版被
提前打掉了。取「公布日 ≤ 新版公布日、施行日 ≤ 今天（或为空）」的最近一个
历史版本恢复为现行，并解冻它的条文。

用法：
  python tools/fix_pending_supersede.py                # 只出报告（默认）
  python tools/fix_pending_supersede.py --apply        # 真正写库
  python tools/fix_pending_supersede.py --db data/law_data.db --apply
"""
from __future__ import annotations

import argparse
import datetime
import os
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "ingest"))

from schema import (  # noqa: E402
    STATUS_EFFECTIVE,
    STATUS_PENDING,
    STATUS_REPEALED,
    STATUS_SUPERSEDED,
    ensure_schema,
)

# update.py 与本脚本共用这一个原因字符串，两条路径产出同一状态
SUPERSEDE_REASON = "被新修订版本取代"


def find_breaks(conn: sqlite3.Connection, today: str) -> list[dict]:
    """列出「有待生效新版、却没有现行版本」的法，以及该恢复哪一版。"""
    pending = conn.execute(
        "SELECT id, norm_name, law_name, publish_date, effective_date "
        "FROM law_versions WHERE status=? ORDER BY id",
        (STATUS_PENDING,),
    ).fetchall()

    out: list[dict] = []
    for pid, norm, _law, ppub, peff in pending:
        has_eff = conn.execute(
            "SELECT 1 FROM law_versions WHERE norm_name=? AND status=? LIMIT 1",
            (norm, STATUS_EFFECTIVE),
        ).fetchone()
        if has_eff:
            continue  # 旧版还在，没坏
        cand = conn.execute(
            "SELECT id, publish_date, effective_date FROM law_versions "
            "WHERE norm_name=? AND id<>? AND status=? "
            "  AND COALESCE(publish_date,'') <= ? "
            "  AND (COALESCE(effective_date,'')='' OR effective_date <= ?) "
            "ORDER BY COALESCE(publish_date,'') DESC, id DESC LIMIT 1",
            (norm, pid, STATUS_SUPERSEDED, ppub or "", today),
        ).fetchone()
        if not cand:
            continue  # 真的没有前版（全新法规），返回 pending 正文是对的
        oid, opub, oeff = cand
        frozen = conn.execute(
            "SELECT COUNT(*) FROM law_articles WHERE version_id=? AND status=? "
            "  AND (repeal_reason=? OR repeal_reason IS NULL)",
            (oid, STATUS_REPEALED, SUPERSEDE_REASON),
        ).fetchone()[0]
        total = conn.execute(
            "SELECT COUNT(*) FROM law_articles WHERE version_id=?", (oid,)
        ).fetchone()[0]
        out.append({
            "norm_name": norm,
            "pending_id": pid,
            "pending_publish": ppub or "",
            "pending_effective": peff or "",
            "restore_id": oid,
            "restore_publish": opub or "",
            "restore_effective": oeff or "",
            "restore_total": total,
            "restore_frozen": frozen,
        })
    return out


def apply_fix(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """把旧版恢复为现行，并解冻条文。返回解冻的条文数。"""
    thawed = 0
    with conn:  # 单事务
        for r in rows:
            conn.execute(
                "UPDATE law_versions SET status=?, superseded_by=NULL, superseded_at=NULL "
                "WHERE id=?",
                (STATUS_EFFECTIVE, r["restore_id"]),
            )
            thawed += conn.execute(
                "UPDATE law_articles SET status=?, repeal_date=NULL, replaced_by=NULL, "
                "  repeal_reason=NULL "
                "WHERE version_id=? AND status=? "
                "  AND (repeal_reason=? OR repeal_reason IS NULL)",
                (STATUS_EFFECTIVE, r["restore_id"], STATUS_REPEALED, SUPERSEDE_REASON),
            ).rowcount
    return thawed


def main() -> None:
    ap = argparse.ArgumentParser(description="修复待生效版本提前作废旧版的数据")
    ap.add_argument("--db", default=os.path.join(_HERE, "..", "data", "law_data.db"))
    ap.add_argument("--apply", action="store_true", help="真正写库；默认只出报告")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        raise SystemExit(f"找不到库文件：{args.db}")

    today = datetime.date.today().isoformat()
    conn = sqlite3.connect(args.db)
    ensure_schema(conn)

    rows = find_breaks(conn, today)
    if not rows:
        print("没有需要修复的法规：所有「已公布未生效」的版本，其前版都仍在现状态。")
        return

    print(f"基准日 {today}｜发现 {len(rows)} 部法的新版尚未生效、旧版却被提前作废：\n")
    for r in rows:
        print(f"  {r['norm_name']}")
        print(f"    待生效 v{r['pending_id']}  公布 {r['pending_publish'] or '—'}"
              f"  施行 {r['pending_effective'] or '—'}")
        print(f"    恢复 v{r['restore_id']}  公布 {r['restore_publish'] or '—'}"
              f"  施行 {r['restore_effective'] or '—'}"
              f"  解冻 {r['restore_frozen']}/{r['restore_total']} 条")

    if not args.apply:
        print("\n以上为预览。确认无误后加 --apply 写库。")
        conn.close()
        return

    thawed = apply_fix(conn, rows)
    conn.close()
    print(f"\n已修复 {len(rows)} 部法，解冻条文 {thawed} 条。")


if __name__ == "__main__":
    main()
