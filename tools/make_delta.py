# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""把「变更账本」里所有法规的当前有效全文打成累积增量包，供已装库的用户本地应用。

和「一版配一版」的增量不同，这里不要求用户的本地版本正好等于上一次发版：
包里放的是账本内每部法规的**最终态全文**，用户的库无论停在哪一版，
只要不早于 min_version，把这些法规整部替换掉就是最新的。

  用户 A 装了 10 月的库 → 下 11 月的包 → 3 MB
  用户 B 装了 8 月的库  → 下 11 月的包 → 还是 3 MB（不是全量 658 MB）

同一部法在账本里改过多次，包里只存最终态一份，所以体积随「改过多少部法」增长，
不随发版次数叠加。

用法：
  # 并入本次变更并出包（发版前的标准动作）
  python tools/make_delta.py --plan reports/changes-xxx.plan.json --out dist/delta-2026.11.30.1.json

  # 只按现有账本重新出包（账本没变、只是重打一次）
  python tools/make_delta.py --out dist/delta-2026.11.30.1.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import date


_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "ingest"))
sys.path.insert(0, _HERE)

from schema import SCHEMA_VERSION, ensure_schema, meta_get  # noqa: E402

DEFAULT_LEDGER = os.path.normpath(os.path.join(_HERE, "..", "state", "change-ledger.json"))


def _open_source_db(path: str) -> sqlite3.Connection:
    """打开源库；schema 已是最新时只读打开，避免生成增量包时改动正式库。

    ensure_schema 会顺手跑 refresh_pending_status，按当天日期改写状态；
    正式库本来就是目标版本，生成包不该反过来写它（否则整库 sha 会变）。
    """
    try:
        probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        row = probe.execute(
            "SELECT value FROM corpus_meta WHERE key='schema_version'").fetchone()
        probe.close()
        schema_ok = bool(row) and int(row[0] or 0) >= SCHEMA_VERSION
    except (sqlite3.OperationalError, ValueError):
        schema_ok = False
    if schema_ok:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if not schema_ok:
        ensure_schema(conn)
    return conn


def load_ledger(path: str) -> dict:
    if not os.path.exists(path):
        return {
            "schema": 1,
            # 账本最后一次并入的是哪一版库。发布门禁 G10 靠它判断「本轮变更是否
            # 已进账本」——没有这个字段时，delta 能正常生成、体积也正常，但内容
            # 缺了整轮变更，且没有任何检查能发现（本轮就漏过一次）。
            "corpus_version": "",
            "min_version": "",
            "since": date.today().isoformat(),
            "updated_at": "",
            "laws": {},
        }
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _version_tuple(value: str) -> tuple:
    """把 YYYY.MM.DD.N 拆成可比较的元组；格式不合规时返回空元组（视为最旧）。"""
    try:
        return tuple(int(x) for x in (value or "").split("."))
    except ValueError:
        return ()


# 版本指纹用到的字段：只收「业务数据」，不收本机自增 id、批次号、写入时间。
# law_level/law_province/law_city 是 schema 4 新增的层级列，旧库回放时是未回填态，
# 按验收口径不参与比对，也不在增量包里携带（覆盖时保留本地原值）。
VERSION_FP_FIELDS = (
    "law_name", "norm_name", "law_type", "law_type_label", "publish_date", "effective_date",
    "issuing_authority", "source_file", "file_sha256", "article_count", "status", "superseded_at",
)
ARTICLE_FP_FIELDS = (
    "law_name", "law_type", "law_type_label", "article_number", "article_number_num",
    "article_suffix", "chapter", "section", "article", "content", "content_hash",
    "publish_date", "effective_date", "issuing_authority", "status", "repeal_date",
    "repeal_reason", "replaced_by", "is_principle_clause", "source_file",
)
VERSION_DUMP_FIELDS = VERSION_FP_FIELDS
ARTICLE_DUMP_FIELDS = ARTICLE_FP_FIELDS


def version_fingerprint(version: dict, articles: list[dict]) -> str:
    """用内容指纹判断「这一版在两端是否已经一致」。

    只序列化业务字段并排序条文，保证同一份数据在不同机器、不同插入顺序下
    算出同一个值。superseded_by 存的是本机自增 id，跨库不可比，改用
    superseded_by_ref（接班版本的公布日）参与指纹。
    """
    payload = {k: version.get(k) for k in VERSION_FP_FIELDS}
    payload["superseded_by_ref"] = version.get("superseded_by_ref") or ""
    payload["articles"] = [
        {k: a.get(k) for k in ARTICLE_FP_FIELDS}
        for a in sorted(articles, key=lambda x: (x.get("article_number_num") or 0,
                                                 x.get("article_suffix") or ""))
    ]
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _dump_articles(conn: sqlite3.Connection, version_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM law_articles WHERE version_id=? "
        "ORDER BY article_number_num, COALESCE(article_suffix, '')",
        (version_id,),
    ).fetchall()
    return [{k: a[k] for k in ARTICLE_DUMP_FIELDS} for a in rows]


def _superceded_by_ref(conn: sqlite3.Connection, v: sqlite3.Row,
                       base_conn: sqlite3.Connection | None = None) -> str:
    """把 superseded_by（本机自增 id）翻译成跨库可比的公布日。"""
    sid = v["superseded_by"]
    if not sid:
        return ""
    row = conn.execute("SELECT publish_date FROM law_versions WHERE id=?", (sid,)).fetchone()
    if row is None and base_conn is not None:
        row = base_conn.execute(
            "SELECT publish_date FROM law_versions WHERE id=?", (sid,)).fetchone()
    return (row[0] or "") if row else ""


def _build_version_entry(conn: sqlite3.Connection, v: sqlite3.Row,
                         base_conn: sqlite3.Connection | None = None) -> dict:
    articles = _dump_articles(conn, v["id"])
    entry = {k: v[k] for k in VERSION_DUMP_FIELDS}
    entry.update({
        "db_id": v["id"],
        "sha256": v["file_sha256"] or "",
        "superseded_by": v["superseded_by"],
        "superseded_by_ref": _superceded_by_ref(conn, v, base_conn),
        "articles": articles,
    })
    entry["version_fp"] = version_fingerprint(entry, articles)
    return entry


def dump_versions(conn: sqlite3.Connection, norm_name: str,
                  base_conn: sqlite3.Connection | None = None) -> list[dict]:
    """把一部法的**全部版本**（含 superseded 历史版）还原成增量包条目。

    旧版只导 effective/pending，导致「residue 清理改了 superseded 历史版本」
    这类修复永远传不到用户库里，回放后历史条文与正式库对不上。这里全状态导出，
    由 apply 端按 version_fp 决定是跳过还是原位覆盖。

    按公布日升序返回；apply 先落旧版再落新版，接班指针的解析顺序才稳定。
    """
    versions = conn.execute(
        "SELECT * FROM law_versions WHERE norm_name=? "
        "ORDER BY COALESCE(publish_date, '') ASC, id ASC",
        (norm_name,),
    ).fetchall()
    return [_build_version_entry(conn, v, base_conn) for v in versions]


def is_version_changed(base_conn: sqlite3.Connection, law: dict) -> bool:
    """这条版本相对基线库是否有任何变化（含条文、状态、接管指针）。

    基线库优先按 db_id 定位；找不到时退回「公布日 + 施行日 + 源文件」匹配，
    避免把基线里被重切过的版本误判成新增而重复插入。
    """
    v = None
    db_id = law.get("db_id")
    if db_id:
        row = base_conn.execute(
            "SELECT * FROM law_versions WHERE id=? AND norm_name=?",
            (db_id, law.get("norm_name"))).fetchone()
        if row is not None:
            v = row
    if v is None:
        v = base_conn.execute(
            "SELECT * FROM law_versions WHERE norm_name=? AND COALESCE(publish_date,'')=? "
            "AND COALESCE(effective_date,'')=? AND COALESCE(source_file,'')=? "
            "ORDER BY id LIMIT 1",
            (law.get("norm_name"), law.get("publish_date") or "",
             law.get("effective_date") or "", law.get("source_file") or "")).fetchone()
    if v is None:
        return True
    base_entry = _build_version_entry(base_conn, v, base_conn)
    return base_entry["version_fp"] != law.get("version_fp")


def is_repealed(conn: sqlite3.Connection, norm_name: str) -> bool:
    """这部法是不是整部废止了：一条现行版、一条待生效版都不剩。"""
    row = conn.execute(
        "SELECT SUM(status IN ('effective', 'pending')) FROM law_versions WHERE norm_name=?",
        (norm_name,),
    ).fetchone()
    return bool(row) and not (row[0] or 0)


def list_all_repealed(conn: sqlite3.Connection) -> list[str]:
    """全库扫出整部废止的法规。扫全库而不只看账本：废止是累积事实，
    账本只记「改过哪几部」，一条现行版都不剩的法未必进过账本。"""
    return [r[0] for r in conn.execute(
        "SELECT norm_name FROM law_versions GROUP BY norm_name "
        "HAVING SUM(status IN ('effective', 'pending')) = 0 ORDER BY norm_name"
    ).fetchall()]


def dump_repealed(conn: sqlite3.Connection, norm_name: str) -> dict | None:
    """整部废止的法规只导一条标记，**不带正文**。

    用户本地若已有这部法，apply 端要把它降级、条文标废止；本地没有就什么都
    不用做——没人需要在自己库里「新增一部已废止的法」。所以正文一个字节都
    不必传：这 44 部合计 2550 条正文，塞进包会把 1.4 MB 撑到 5 MB 以上，
    而 apply 端一个字都用不上。

    废止日期和依据记在 law_articles 上（law_versions 没有这两列），取最新
    版本里任一条非空记录即可——同一部法的所有条文是同一批标的。
    """
    ver = conn.execute(
        "SELECT id, law_name, norm_name, law_type, law_type_label, publish_date, "
        "       effective_date, issuing_authority, source_file "
        "FROM law_versions WHERE norm_name=? "
        "ORDER BY COALESCE(publish_date, '') DESC, id DESC LIMIT 1",
        (norm_name,),
    ).fetchone()
    if ver is None:
        return None

    mark = conn.execute(
        "SELECT repeal_date, repeal_reason FROM law_articles "
        "WHERE version_id=? AND repeal_date IS NOT NULL AND repeal_date != '' LIMIT 1",
        (ver["id"],),
    ).fetchone()
    n_articles = conn.execute(
        "SELECT COUNT(*) FROM law_articles WHERE version_id=?", (ver["id"],)
    ).fetchone()[0]

    return {
        "kind": "repealed",
        "law_name": ver["law_name"],
        "norm_name": ver["norm_name"],
        "law_type": ver["law_type"],
        "law_type_label": ver["law_type_label"],
        "publish_date": ver["publish_date"],
        "effective_date": ver["effective_date"],
        "issuing_authority": ver["issuing_authority"],
        "repeal_date": (mark["repeal_date"] if mark else "") or "",
        "repeal_reason": (mark["repeal_reason"] if mark else "") or "",
        "article_count": n_articles,
    }


def merge_plan(ledger: dict, plan_path: str) -> tuple[int, int, int]:
    """把本次变更并入账本。返回（新增条目，刷新日期，跳过）。

    只收 new / update。stale 是「镜像版本比库里旧」的，收进账本等于把用户
    本地的新版降级回旧版——这正是原来那份增量包会干的事（实测混进 97 部）。
    """
    with open(plan_path, encoding="utf-8") as f:
        plan = json.load(f)

    added = refreshed = skipped = 0
    for law in plan["laws"]:
        action = law.get("action")
        if action not in ("new", "update"):
            skipped += 1
            continue
        norm = law.get("norm_name")
        if not norm:
            continue
        stamp = law.get("publish_date") or date.today().isoformat()
        old = ledger["laws"].get(norm)
        if old is None:
            ledger["laws"][norm] = stamp
            added += 1
        elif stamp > old:
            ledger["laws"][norm] = stamp
            refreshed += 1
    return added, refreshed, skipped


def main() -> None:
    ap = argparse.ArgumentParser(description="生成累积增量包")
    ap.add_argument("--plan", action="append", default=None,
                    help="本次变更计划（可选，可重复传多次，按顺序并入同一本账）")
    ap.add_argument("--db", default="./data/law_data.db")
    ap.add_argument("--ledger", default=DEFAULT_LEDGER)
    ap.add_argument("--out", required=True)
    ap.add_argument("--base-db", default=None,
                    help="上一版基线库快照；给了才导出 superseded 历史版的修正与删除版清单")
    ap.add_argument("--no-ledger-write", action="store_true", help="只读账本，不写回")
    args = ap.parse_args()

    ledger = load_ledger(args.ledger)

    if args.plan:
        ids = []
        for plan_path in args.plan:
            added, refreshed, skipped = merge_plan(ledger, plan_path)
            print(f"账本并入 {os.path.basename(plan_path)}：新增 {added} 部，刷新 {refreshed} 部，"
                  f"忽略 {skipped} 条（skip/stale）")
            ids.append(os.path.basename(plan_path))
        if len(ids) > 1:
            print(f"  共 {len(ids)} 份计划并账，账本现有 {len(ledger['laws'])} 部")

    if not ledger["laws"]:
        raise SystemExit("账本里没有法规，增量包会是空的。先带 --plan 跑一次把账本建起来。")

    conn = _open_source_db(args.db)

    base_conn = None
    if args.base_db:
        if not os.path.exists(args.base_db):
            raise SystemExit(f"基线库不存在：{args.base_db}")
        base_conn = sqlite3.connect(f"file:{args.base_db}?mode=ro", uri=True)
        base_conn.row_factory = sqlite3.Row

    to_version = meta_get(conn, "corpus_version", "0")
    data_cutoff = meta_get(conn, "data_cutoff", "")
    if not ledger.get("min_version"):
        # 首次建账本：起点就是当前这版，所有装了这版的人以后都能走增量
        ledger["min_version"] = to_version
    if not ledger.get("since"):
        ledger["since"] = date.today().isoformat()

    laws = []
    missing = []
    empty = []
    for norm in sorted(ledger["laws"]):
        dumps = dump_versions(conn, norm, base_conn)
        if not dumps:
            # 账本里有、但库里一条记录都不剩（极端情况），报缺失
            if not is_repealed(conn, norm):
                missing.append(norm)
            continue
        for law in dumps:
            if not law["articles"]:
                empty.append(norm)
                continue
            # 给了基线快照时，只带「相对基线确实有变化」的版本 —— 增量包的
            # 起点就是那次全量包，没变过的版本没必要重复发（否则包体翻倍）。
            # 没给基线快照时退回老口径：只带现行/待生效版，供任意旧库收敛。
            if base_conn is not None:
                if not is_version_changed(base_conn, law):
                    continue
            elif law["status"] not in ("effective", "pending"):
                continue
            laws.append(law)

    # 整部废止单独扫全库：这类法进不了上面的循环（没有现行版可取），
    # 但如果只靠全量包传播，走增量的用户会永远留着一部已废止的「现行法」。
    repealed = [x for x in (dump_repealed(conn, n) for n in list_all_repealed(conn)) if x]

    # 版本删除：基线有、当前没有的版本（如牡丹江餐厨废弃物管理条例被清掉的
    # 2026-09-28 版）。没有删除语义，用户库里会永远多一条已不存在的版本。
    deleted = []
    if base_conn is not None:
        cur_ids = {r[0] for r in conn.execute("SELECT id FROM law_versions")}
        for v in base_conn.execute(
                "SELECT id, law_name, norm_name, publish_date, effective_date, "
                "       source_file, status, article_count FROM law_versions ORDER BY id"):
            if v["id"] in cur_ids:
                continue
            deleted.append({
                "db_id": v["id"],
                "law_name": v["law_name"],
                "norm_name": v["norm_name"],
                "publish_date": v["publish_date"],
                "effective_date": v["effective_date"],
                "source_file": v["source_file"],
                "status": v["status"],
                "article_count": v["article_count"],
            })
    if base_conn is not None:
        base_conn.close()
    conn.close()

    if missing:
        print(f"警告：{len(missing)} 部在库里既无生效版也无待生效版，未进包：{', '.join(missing[:8])}"
              + (" …" if len(missing) > 8 else ""))
    if empty:
        print(f"警告：{len(empty)} 部切不出条文，未进包：{', '.join(empty[:8])}"
              + (" …" if len(empty) > 8 else ""))

    if not laws and not repealed and not deleted:
        raise SystemExit("一部法规都没取到，拒绝生成空包。")

    total_articles = sum(len(x["articles"]) for x in laws)
    delta = {
        "schema": 3,
        "min_version": ledger["min_version"],
        "to_version": to_version,
        "data_cutoff": data_cutoff,
        "generated_at": date.today().isoformat(),
        "ledger_since": ledger.get("since", ""),
        "laws": laws,
        "repealed": repealed,
        "deleted_versions": deleted,
        "summary": {
            "laws": len(laws),
            "articles": total_articles,
            "repealed": len(repealed),
            "deleted_versions": len(deleted),
        },
    }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(delta, f, ensure_ascii=False, indent=1)

    size_mb = os.path.getsize(args.out) / 1048576
    pending_n = sum(1 for x in laws if (x.get("effective_date") or "") > date.today().isoformat())
    print(f"累积增量包：{args.out}")
    print(f"  {len(laws)} 条版本 / {total_articles} 条正文 ｜ {size_mb:.1f} MB 未压缩")
    if repealed:
        n_art = sum(x["article_count"] for x in repealed)
        print(f"  另含 {len(repealed)} 部整部废止（{n_art} 条正文随之失效，只带标记不带正文）")
    if deleted:
        print(f"  另含 {len(deleted)} 个删除版本（用户库里对应的旧版会被清掉）")
    if pending_n:
        print(f"  其中 {pending_n} 条是待生效版本（带进包，用户那天翻成现行）")
    print(f"  适用：本地版本 ≥ {ledger['min_version']}（更早的会回落全量）")
    print(f"  目标：{to_version}，条文截至 {data_cutoff or '未知'}")

    if not args.no_ledger_write:
        ledger["updated_at"] = date.today().isoformat()
        ledger["laws"] = {k: ledger["laws"][k] for k in sorted(ledger["laws"])}
        # 只在版本前进时才写：拿旧库跑一次探针（比如拿 11.03.3 基线验证 delta）
        # 不该把账本的 corpus_version 降回去，否则 G10 会对当前库误判成「账本未更新」。
        old_cv = (ledger.get("corpus_version") or "").strip()
        if not old_cv or _version_tuple(to_version) >= _version_tuple(old_cv):
            ledger["corpus_version"] = to_version
        os.makedirs(os.path.dirname(os.path.abspath(args.ledger)), exist_ok=True)
        with open(args.ledger, "w", encoding="utf-8") as f:
            json.dump(ledger, f, ensure_ascii=False, indent=1)
        print(f"  账本已更新：{args.ledger}（{len(ledger['laws'])} 部，"
              f"corpus_version={ledger.get('corpus_version') or '未设置'}）")


if __name__ == "__main__":
    main()
