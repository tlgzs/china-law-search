# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""把「废止决定」的连带效力落到库里。

背景：废止决定本身是一部法（类型为「修改决定」，如《海南省人民代表大会常务
委员会关于废止〈海南省环境保护条例〉的决定》），入库后可以检索到；但被它废止
的那部法（《海南省环境保护条例》）在库里仍然标着现行有效。律师照库查，会查到
一部已经不存在的法规，而且引用起来看不出任何异常提示。

这个脚本从决定名称里解析出被废止的法规名，在库里找到对应版本，把版本降为
superseded、条文标 repealed 并写明废止依据与日期。

判定规则（都偏保守，宁可漏也不误伤）：
- 只处理名称形如「…关于废止〈法规名〉的决定」的现行版本；
- 「关于废止部分/若干行政法规的决定」这类笼统决定无法定位具体法规，跳过；
- 决定名称里带「和修改」的，只取「废止」那一段，不动被修改的法规；
- 目标法规必须在库里有现行版本，且**没有比决定更晚的版本**
  （有更晚版本说明这部法后来重新制定过，不能按废止处理）；
- 已经 superseded 的版本跳过，重复跑不会出问题。

用法：
  python tools/mark_repealed_by_decisions.py              # 只出报告（默认）
  python tools/mark_repealed_by_decisions.py --apply      # 真正写库
  python tools/mark_repealed_by_decisions.py --show 40    # 明细多打几行
  python tools/mark_repealed_by_decisions.py --db /path/to/law_data.db --apply

写库时会在 state/ 下留一份回滚清单（记录动了哪些版本、改动前的状态），
反悔时按它恢复即可。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "ingest"))

from schema import (  # noqa: E402
    STATUS_EFFECTIVE,
    STATUS_REPEALED,
    STATUS_SUPERSEDED,
    ensure_schema,
)

# 「等两件」「等 3 部」这类尾巴，抽法规名时要先切掉
_NUM = "一二三四五六七八九十百零两几"
_TAIL_RE = re.compile(rf"等[{_NUM}\d]+(?:件|部)[^，,。]*$")
# 笼统决定：管不到具体哪一部，跳过
_GENERIC_RE = re.compile(r"部分|若干")


def decision_targets(norm_name: str) -> tuple:
    """从决定名称里解析被废止的法规名。

    返回 (法规名列表, 解析方式)。列表为空表示这条决定定位不到具体法规。
    """
    m = re.search(r"关于(?:废止和修改|修改和废止|废止)(.*?)的决定$", norm_name)
    if not m:
        return [], "no-pattern"

    body = m.group(1).strip()
    if not body:
        return [], "no-pattern"

    inside = re.findall(r"《([^》]+)》", body)
    if inside:
        return [x.strip() for x in inside if x.strip()], "bookmark"

    body = _TAIL_RE.sub("", body).strip()
    body = re.split(r"和修改|以及修改", body)[0].strip()
    if not body:
        return [], "no-pattern"
    if _GENERIC_RE.search(body):
        return [], "generic"
    return [body], "plain"


def collect(conn: sqlite3.Connection, today: str) -> dict:
    """扫描全部现行废止决定，算出能落地的改动。只读。"""
    decisions = conn.execute(
        "SELECT id, norm_name, law_name, publish_date, effective_date FROM law_versions "
        "WHERE status=? AND law_type_label='修改决定' AND norm_name LIKE '%废止%' "
        "ORDER BY norm_name",
        (STATUS_EFFECTIVE,),
    ).fetchall()

    stats = {"decisions": len(decisions), "no-pattern": 0, "generic": 0,
             "bookmark": 0, "plain": 0}
    plan = []
    skipped = []

    for d in decisions:
        names, how = decision_targets(d["norm_name"])
        if not names:
            stats[how] += 1
            skipped.append({"decision": d["norm_name"], "why": how})
            continue
        stats[how] += 1

        repeal_date = d["effective_date"] or d["publish_date"] or ""
        for name in names:
            target = conn.execute(
                "SELECT id, law_name, norm_name, publish_date, effective_date FROM law_versions "
                "WHERE norm_name=? AND status=? LIMIT 1",
                (name, STATUS_EFFECTIVE),
            ).fetchone()

            if target is None:
                already = conn.execute(
                    "SELECT 1 FROM law_versions v JOIN law_articles a ON a.version_id = v.id "
                    "WHERE v.norm_name=? AND a.repeal_reason LIKE '被%废止' LIMIT 1",
                    (name,),
                ).fetchone()
                skipped.append({
                    "decision": d["norm_name"], "target": name,
                    "why": "已是废止状态" if already else "库里没有该法规的现行版本",
                })
                continue

            later = conn.execute(
                "SELECT publish_date FROM law_versions "
                "WHERE norm_name=? AND COALESCE(publish_date,'') > ? LIMIT 1",
                (name, d["publish_date"] or ""),
            ).fetchone()
            if later is not None:
                skipped.append({"decision": d["norm_name"], "target": name,
                                "why": f"该法规有更晚版本（{later['publish_date']}），"
                                       "应另有现行文本，不按废止处理"})
                continue

            if repeal_date and repeal_date > today:
                skipped.append({"decision": d["norm_name"], "target": name,
                                "why": f"废止决定 {repeal_date} 才生效"})
                continue

            # 有已公布未施行的新版，说明立法的走向是「换一版继续用」而不是「不要了」，
            # 两者同时出现多半是解析错了，不动，留给人看。
            pending_new = conn.execute(
                "SELECT publish_date FROM law_versions WHERE norm_name=? AND status='pending' LIMIT 1",
                (name,),
            ).fetchone()
            if pending_new is not None:
                skipped.append({"decision": d["norm_name"], "target": name,
                                "why": f"该法规另有待生效新版（{pending_new['publish_date']}），"
                                       "与废止判定冲突"})
                continue

            article_count = conn.execute(
                "SELECT COUNT(*) FROM law_articles WHERE version_id=?",
                (target["id"],),
            ).fetchone()[0]

            plan.append({
                "version_id": target["id"],
                "norm_name": name,
                "law_name": target["law_name"],
                "decision_id": d["id"],
                "decision_name": d["norm_name"],
                "repeal_date": repeal_date,
                "articles": article_count,
                "reason": f"被{d['law_name']}废止",
            })

    return {"stats": stats, "plan": plan, "skipped": skipped}


def apply_plan(conn: sqlite3.Connection, plan: list, out_dir: str) -> str:
    """真正写库：版本降 superseded，条文标 repealed。返回回滚清单路径。"""
    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    rollback = {
        "generated_at": datetime.datetime.now().replace(microsecond=0).isoformat(),
        "note": "按 versions[].before_status 把版本改回去，条文按其 version_id 批量恢复为 effective",
        "versions": [
            {"version_id": p["version_id"], "norm_name": p["norm_name"],
             "before_status": STATUS_EFFECTIVE, "decision": p["decision_name"]}
            for p in plan
        ],
    }

    with conn:
        for p in plan:
            conn.execute(
                "UPDATE law_versions SET status=?, superseded_by=?, superseded_at=? WHERE id=?",
                (STATUS_SUPERSEDED, p["decision_id"], p["repeal_date"], p["version_id"]),
            )
            conn.execute(
                "UPDATE law_articles SET status=?, repeal_date=?, repeal_reason=? WHERE version_id=?",
                (STATUS_REPEALED, p["repeal_date"], p["reason"], p["version_id"]),
            )

    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"repeal-rollback-{stamp}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rollback, f, ensure_ascii=False, indent=1)
    return path


def main() -> None:
    ap = argparse.ArgumentParser(description="按废止决定标记被废止的法规")
    ap.add_argument("--db", default="./data/law_data.db")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只出报告）")
    ap.add_argument("--show", type=int, default=15, help="明细打印条数")
    ap.add_argument("--out-dir", default="./state", help="回滚清单写到哪")
    args = ap.parse_args()

    today = datetime.date.today().isoformat()
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    result = collect(conn, today)
    stats, plan, skipped = result["stats"], result["plan"], result["skipped"]

    print(f"扫描现行废止决定：{stats['decisions']} 条")
    print(f"  能定位到具体法规：{stats['bookmark'] + stats['plain']} 条"
          f"（书名号 {stats['bookmark']} / 直接写名 {stats['plain']}）")
    print(f"  笼统决定（部分、若干）：{stats['generic']} 条，跳过")
    print(f"  名称不含「关于废止…的决定」：{stats['no-pattern']} 条，跳过")

    total_articles = sum(p["articles"] for p in plan)
    print()
    print(f"可标记为已废止：{len(plan)} 部法规 / {total_articles} 条条文")
    if plan:
        print()
        show = plan if args.show <= 0 else plan[: args.show]
        for p in show:
            print(f"  {p['norm_name'][:40]:42} {p['articles']:>5} 条  "
                  f"废止日 {p['repeal_date'] or '(未写)'}")
        if len(plan) > len(show):
            print(f"  …另有 {len(plan) - len(show)} 部")

    other = [s for s in skipped if s["why"] not in ("no-pattern", "generic")]
    done = [s for s in other if s["why"] == "已是废止状态"]
    pending_review = [s for s in other if s["why"] != "已是废止状态"]

    if done:
        print()
        print(f"已是废止状态、无需再动：{len(done)} 条")

    if pending_review:
        print()
        print(f"未处理 {len(pending_review)} 条（需要人工确认）：")
        for s in pending_review[: args.show]:
            print(f"  {(s.get('target') or s['decision'])[:38]:40} {s['why']}")

    if not args.apply:
        print()
        print("（只出报告。确认无误后加 --apply 写库。）")
        conn.close()
        return

    if not plan:
        print()
        print("没有需要写的改动。")
        conn.close()
        return

    path = apply_plan(conn, plan, args.out_dir)
    print()
    print(f"已写库：{len(plan)} 个版本降为 superseded，{total_articles} 条条文标为 repealed")
    print(f"回滚清单：{path}")
    conn.close()


if __name__ == "__main__":
    main()
