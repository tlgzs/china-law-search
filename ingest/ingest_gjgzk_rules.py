# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""把国家规章库（gjgzk）已落盘的规章 Markdown 增量入库 SQLite。

与 ingest/build_sqlite_from_markdown.py 的区别：
  - 数据源是 inbox/gjgzk/md（每部一份，正文由列表接口返回，非 flk 官方格式）；
  - 该源把条文粘成一段，条号靠空白分隔，需要按「第X条 + 空白」切分；
  - 新增两个法规层级（部门规章 / 地方政府规章），写入 law_level；
  - 按批次入库并记录 ingest_batches，可按 batch_id 回滚。

用法：
  # B1：按发布机关分组
  python ingest/ingest_gjgzk_rules.py --category 部门规章 \
      --group 国家市场监督管理总局,交通运输部 --batch-id gjgzk-b1-20261108

  # 只看解析结果，不写库
  python ingest/ingest_gjgzk_rules.py --category 部门规章 --group 交通运输部 --dry-run

  # 回滚某一批
  python ingest/ingest_gjgzk_rules.py --rollback gjgzk-b1-20261108
"""
from __future__ import annotations

import argparse
import datetime
import glob
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from schema import (  # noqa: E402
    content_hash,
    ensure_schema,
    file_sha256,
    index_bigram_articles,
    initial_status,
    norm_law_name,
    now_iso,
)

MD_DIR = "./inbox/gjgzk/md"
META_DIR = "./inbox/gjgzk/docs"
DB_PATH = "./data/law_data.db"

# 条号字符集：本源的条号只用中文数字或阿拉伯数字，不含「之一」后缀。
NUM_CHARS = "零〇一二三四五六七八九十百千万亿两0-9"
ARTICLE_RE = re.compile("第([" + NUM_CHARS + "]+)条")
# 正文被压成一段时段落之间只剩空白；这些字符算「上一段结束」与「新条开始」的边界。
WS = set(" \t\r\n\u3000" + "".join(chr(c) for c in range(0x2000, 0x200b)))
DOTTED_RE = re.compile(r"第\s*\d+\.\d+\s*条")

_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10000, "亿": 100000000}

_CATEGORY_MAP = {
    "部门规章": ("department_rule", "部门规章", "national"),
    "地方政府规章": ("local_government_rule", "地方政府规章", "province"),
}

_ORDER_NO_RE = re.compile(r"令\s*(?:[（(]?\s*(\d{4})\s*年\s*[）)]?\s*)?第?\s*(\d+)\s*号")
_DATE_RE = re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日")
_META_LINE_RE = re.compile(r"^-\s*(.+?)[:：]\s*(.*)$")


def cn_to_int(text: str) -> int:
    if text.isdigit():
        return int(text)
    result = 0
    temp = 0
    for ch in text:
        if ch in _CN_DIGIT:
            temp = _CN_DIGIT[ch]
        elif ch in _CN_UNIT:
            if temp == 0:
                temp = 1
            result += temp * _CN_UNIT[ch]
            temp = 0
        else:
            return 0
    return result + temp


def parse_header(text: str) -> dict:
    """解析 md 头部「- 键：值」块。"""
    head = text.split("## 正文", 1)[0]
    out: dict[str, str] = {}
    for line in head.split("\n"):
        m = _META_LINE_RE.match(line.strip())
        if m:
            out[m.group(1).strip()] = m.group(2).strip()
    return out


def split_articles(body: str) -> tuple[list[dict], list[int], list[int]]:
    """切条文，返回 (可用条文, 源侧缺号, 重复条号)。

    只把「前一个字符是空白」的「第X条」当作条文起点：正文里大量「依照广告法
    第五十七条」这类引用，前面紧挨着汉字，不能切；而真空行/空格分隔的条文
    起点前面一定是空白。上海、重庆等地方规章写作「第一条（目的和依据）」，
    条号后面紧跟括号，不影响切分。
    """
    starts = [
        m for m in ARTICLE_RE.finditer(body)
        if m.start() == 0 or body[m.start() - 1] in WS
    ]
    articles: list[dict] = []
    duplicates: list[int] = []
    seen: set[int] = set()
    i = 0
    while i < len(starts):
        m = starts[i]
        num = cn_to_int(m.group(1))
        j = i + 1
        end = starts[j].start() if j < len(starts) else len(body)
        content = body[m.end():end].strip()
        # 本条与下一条起点之间是空的，说明紧随其后的「第X条」其实是本条正文
        # 开头的引用（如「第九条 第七条 和第八条 所述…」），不是新的条文起点。
        # 把区间继续向后延到下一个有内容的起点，中间这些引用起点一并跳过，
        # 正文归入本条，既不丢条文也不产生假缺口。
        while j < len(starts) and not content:
            j += 1
            end = starts[j].start() if j < len(starts) else len(body)
            content = body[m.end():end].strip()
        i = max(j, i + 1)
        number = m.group(0)
        if num <= 0 or not content:
            continue
        if num in seen:
            duplicates.append(num)
            continue
        seen.add(num)
        articles.append({"article_number": number, "num": num, "content": content})
    articles.sort(key=lambda a: a["num"])
    max_num = max((a["num"] for a in articles), default=0)
    gaps = [n for n in range(1, max_num + 1) if n not in seen]
    return articles, gaps, duplicates


def split_articles_chained(body: str) -> tuple[list[dict], list[int], list[int]]:
    """按条号 1、2、3… 顺序向后找起点的切分法，专供被压成一段、条号前没有空白的
    地方规章正文使用（如「第一章总则第一条为了…第二条本办法所称…」）。

    做法：先给每个条号收集出现位置，再从第一条起，按「上一条之后第一个出现的该
    条号」逐条向后认。正文里的引用（「依照第二十六条」「根据《…》第十二条」）条号
    会与当前进度错位，因此不会被当成起点；跳过的条号计入缺口，连续缺 3 条即停，
    避免一个错号把后面整篇带偏。
    """
    hits: dict[int, list] = {}
    for m in ARTICLE_RE.finditer(body):
        num = cn_to_int(m.group(1))
        if num > 0:
            hits.setdefault(num, []).append(m)
    starts = []
    last = -1
    miss = 0
    num = 1
    max_num = max(hits) if hits else 0
    while num <= max_num and miss <= 2:
        cand = [m for m in hits.get(num, []) if m.start() > last]
        if not cand:
            miss += 1
            num += 1
            continue
        m = cand[0]
        starts.append(m)
        last = m.start()
        miss = 0
        num += 1

    articles: list[dict] = []
    duplicates: list[int] = []
    seen: set[int] = set()
    i = 0
    while i < len(starts):
        m = starts[i]
        j = i + 1
        end = starts[j].start() if j < len(starts) else len(body)
        content = body[m.end():end].strip()
        while j < len(starts) and not content:
            j += 1
            end = starts[j].start() if j < len(starts) else len(body)
            content = body[m.end():end].strip()
        i = max(j, i + 1)
        number = m.group(0)
        num = cn_to_int(m.group(1))
        if num <= 0 or not content:
            continue
        if num in seen:
            duplicates.append(num)
            continue
        seen.add(num)
        articles.append({"article_number": number, "num": num, "content": content})

    articles.sort(key=lambda a: a["num"])
    max_num = max((a["num"] for a in articles), default=0)
    gaps = [n for n in range(1, max_num + 1) if n not in seen]
    return articles, gaps, duplicates



def extract_order_no(*candidates: str) -> str:
    """取令号；取不到返回空串，不猜。"""
    for text in candidates:
        if not text:
            continue
        m = _ORDER_NO_RE.search(text)
        if m:
            year, num = m.group(1), m.group(2)
            return f"{year}年第{num}号" if year else f"第{num}号"
    return ""


def extract_effective_date(pub_info: str, publish_date: str) -> str:
    m = re.search(r"自\s*(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日\s*起(?:施行|实施)", pub_info or "")
    if m:
        y, mo, d = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
    # 「自公布之日起施行」是明确表述，生效日就等于公布日，不是猜测。
    if re.search(r"自\s*(?:公布|发布)\s*之日起(?:施行|实施)", pub_info or ""):
        return publish_date
    return ""


def extract_publish_date(pub_info: str) -> str:
    """取「公布日期」：只认日期后接令号/公布/发布的写法，其余留空。

    不能拿源的 update_time 顶替：实测 533 部部门规章里 350 部的 update_time
    是最后一次修订日期（如「企业名称登记管理实施办法」公布 2004-06-14、
    update_time 2021-06-24），拿它当公布日期会把版本时间线整体推错。
    发文机关可能列了十几个部委，日期与「令」之间允许较长间隔。
    """
    m = re.search(
        r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日"
        r"[^。；]{0,120}?(?:令[^。；]{0,40}号|公布|发布)",
        pub_info or "",
    )
    if not m:
        return ""
    y, mo, d = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def load_meta(doc_id: str) -> dict | None:
    path = os.path.join(META_DIR, f"{doc_id}.meta.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def collect_documents(category: str, groups: list[str], limit: int = 0,
                      doc_ids: set[str] | None = None,
                      allow_nonumbered: bool = False) -> tuple[list[dict], dict]:
    """读取 md + meta，返回 (待入库文档, 解析统计)。

    doc_ids 非空时只取这些 doc_id，供个别文档的单点补录/修正使用，避免整类重跑。
    allow_nonumbered 只在显式传参时生效：正文没有条号的文书整篇存 1 条，
    条号记「全文」，不虚构第X条。
    """
    group_set = set(groups)
    stats = {"scanned": 0, "matched": 0, "no_articles": 0, "dotted": 0,
             "unreadable": 0, "outside_group": 0, "nonumbered": 0}
    docs: list[dict] = []
    for path in sorted(glob.glob(os.path.join(MD_DIR, "*.md"))):
        doc_id = os.path.basename(path).split("_", 1)[0]
        meta = load_meta(doc_id)
        if not meta:
            stats["unreadable"] += 1
            continue
        if meta.get("category") != category:
            continue
        stats["scanned"] += 1
        group = meta.get("dept_names", "") if category == "部门规章" else meta.get("province", "")
        if group_set and group not in group_set:
            stats["outside_group"] += 1
            continue
        if doc_ids and doc_id not in doc_ids:
            stats["outside_group"] += 1
            continue
        stats["matched"] += 1
        text = open(path, "r", encoding="utf-8").read()
        header = parse_header(text)
        body = text.split("## 正文", 1)[-1]
        articles, gaps, duplicates = split_articles(body)
        if category == "地方政府规章":
            # 地方规章源大量把正文压成一段、条号前没有空白，空白切分只能切出 1 条
            # 甚至 0 条；顺序链切分在 4845 部已可切文档上实测 938 部更准、2 部更差，
            # 故两者取条数更多的一份，不做逐部特判。
            chained, chained_gaps, chained_duplicates = split_articles_chained(body)
            if len(chained) > len(articles):
                articles, gaps, duplicates = chained, chained_gaps, chained_duplicates
        nonumbered = False
        if not articles:
            if allow_nonumbered and body.strip():
                articles = [{"article_number": "全文", "num": 0, "content": body.strip()}]
                gaps, duplicates = [], []
                nonumbered = True
                stats["nonumbered"] += 1
            else:
                stats["no_articles"] += 1
                if DOTTED_RE.search(body):
                    stats["dotted"] += 1
                continue
        law_type, law_type_label, law_level = _CATEGORY_MAP[category]
        if category == "地方政府规章" and meta.get("region"):
            law_level = "city"
        pub_info = meta.get("pub_info", "") or ""
        publish_date = header.get("公布日期", "") or extract_publish_date(pub_info)
        effective_date = header.get("施行日期", "") or extract_effective_date(pub_info, publish_date)
        order_no = extract_order_no(header.get("令号", ""), pub_info)
        issuing_authority = meta.get("issuing_body") or meta.get("dept_names") or ""
        docs.append({
            "doc_id": doc_id,
            "title": meta.get("title", "").strip(),
            "law_type": law_type,
            "law_type_label": law_type_label,
            "law_level": law_level,
            "law_province": meta.get("province", "") if category != "部门规章" else "",
            "law_city": meta.get("region", "") if category != "部门规章" else "",
            "issuing_authority": issuing_authority,
            "publish_date": publish_date,
            "effective_date": effective_date,
            "order_no": order_no,
            "source_url": meta.get("url", ""),
            "source_file": os.path.basename(path),
            "sha256": file_sha256(path),
            "pub_info": pub_info,
            "articles": articles,
            "gaps": gaps,
            "duplicates": duplicates,
            "nonumbered": nonumbered,
            "group": group,
        })
        if limit and len(docs) >= limit:
            break
    return docs, stats


def existing_version(conn: sqlite3.Connection, norm: str, law_type: str,
                     batch_id: str) -> sqlite3.Row | None:
    """查库内是否已有同名版本；本批自己写入的不算冲突。"""
    return conn.execute(
        "SELECT id, law_type, status FROM law_versions WHERE norm_name=? "
        "AND COALESCE(batch_id,'') != ? "
        "ORDER BY COALESCE(publish_date,'') DESC, id DESC LIMIT 1",
        (norm, batch_id),
    ).fetchone()


def _total_len(doc: dict) -> int:
    return sum(len(a["content"]) for a in doc["articles"])


def _amendment_year(doc: dict) -> int:
    """从公布信息里取最后一次修正的年份；没有修正记 0。"""
    years = [int(y) for y in re.findall(r"根据\s*(\d{4})\s*年", doc.get("pub_info", ""))]
    if years:
        return max(years)
    return 1 if "修正" in doc.get("pub_info", "") else 0


def _version_key(doc: dict) -> tuple:
    """判断哪一版更新：公布日 → 修正年份 → 正文完整度 → doc_id。

    公布日缺失时退到施行日（网络安全审查 2022 版就是这样排在 2020 版之后）。
    公布日与施行日都相同时，带「根据…修正」的新修正版优先；再相同则保留正文
    更全的一份（源站对同一版收录有残缺，如行政处罚信息公示规定 19/22 条）。
    """
    return (doc["publish_date"] or doc["effective_date"] or "",
            _amendment_year(doc), _total_len(doc), doc["doc_id"])


def plan_documents(docs: list[dict]) -> tuple[list[list[dict]], list[dict]]:
    """按法规名分组，返回「每组按新旧降序」的文档组，以及被去重掉的条目。

    本次源的同一部规章会出现两条：有的是新旧修订版（公布/施行日期不同），
    有的是同一版被两个来源站各收一次（日期相同、正文长短略差）。
    前者要都入库并保留版本链；后者只留正文更全的一条。全部丢弃会把
    「新修订版」丢掉（食品召回 2026 版就是这样差点被 2015 版顶掉）。
    """
    grouped: dict[str, list[dict]] = {}
    for doc in docs:
        grouped.setdefault(norm_law_name(doc["title"]), []).append(doc)
    groups: list[list[dict]] = []
    dropped: list[dict] = []
    for _, items in grouped.items():
        items.sort(key=_version_key, reverse=True)
        if len(items) > 1 and len({(d["publish_date"], d["effective_date"]) for d in items}) == 1:
            groups.append(items[:1])
            for d in items[1:]:
                dropped.append({"doc_id": d["doc_id"], "title": d["title"],
                                "reason": "同版本重复，保留正文更全的一份"})
            continue
        groups.append(items)
    return groups, dropped


def resolve_version_chain(conn: sqlite3.Connection, infos: list[tuple[dict, int, int]],
                          today: str) -> int:
    """同名的多版本按新旧定状态：新版未生效时旧版继续现行，新版生效后旧版降级。

    schema 里的 _supersede_stale_versions 只按 publish_date 排序，公布日缺失的
    两条会退化成「后写入的赢」（网络安全审查 2022 版一度被 2020 版顶掉）。
    这里用 _version_key 排好的顺序落状态，不再让全局校正回头覆盖。
    """
    if len(infos) < 2:
        return 0
    newest_doc, newest_id, _ = infos[0]
    newest_pending = bool(newest_doc["effective_date"] and newest_doc["effective_date"] > today)
    if newest_pending:
        return 0
    changed = 0
    for doc, vid, _ in infos[1:]:
        changed += conn.execute(
            "UPDATE law_versions SET status='superseded', superseded_by=?, superseded_at=? "
            "WHERE id=? AND status='effective'", (newest_id, now_iso(), vid)).rowcount
        changed += conn.execute(
            "UPDATE law_articles SET status='repealed', repeal_date=?, replaced_by=?, "
            "repeal_reason=? WHERE version_id=? AND status!='repealed'",
            (newest_doc["effective_date"] or today, newest_doc["title"],
             "被新修订版本取代", vid)).rowcount
    return changed


def insert_document(conn: sqlite3.Connection, doc: dict, batch_id: str) -> tuple[int, int]:
    """写一个版本及其条文，返回 (version_id, 条文数)。"""
    status = initial_status(doc["effective_date"])
    cur = conn.execute(
        """INSERT INTO law_versions
           (law_name, norm_name, law_type, law_type_label, publish_date, effective_date,
            issuing_authority, source_file, file_sha256, article_count, status, batch_id,
            created_at, law_level, law_province, law_city, order_no, source_url, source_gap)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (doc["title"], norm_law_name(doc["title"]), doc["law_type"], doc["law_type_label"],
         doc["publish_date"], doc["effective_date"], doc["issuing_authority"],
         doc["source_file"], doc["sha256"], len(doc["articles"]), status, batch_id,
         now_iso(), doc["law_level"], doc["law_province"], doc["law_city"],
         doc["order_no"], doc["source_url"],
         "no_article_numbering" if doc.get("nonumbered") else ",".join(str(n) for n in doc["gaps"]),),
    )
    version_id = int(cur.lastrowid)
    article_ids: list[int] = []
    for art in doc["articles"]:
        ccontent = art["content"]
        acid = conn.execute(
            """INSERT OR REPLACE INTO law_articles
               (vector_id, version_id, law_name, law_type, law_type_label, article_number,
                article_number_num, article_suffix, chapter, section, article, content,
                content_hash, publish_date, effective_date, issuing_authority, status,
                source_file, law_level, law_province, law_city)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"{doc['source_file']}|{art['num']}", version_id, doc["title"],
             doc["law_type"], doc["law_type_label"], art["article_number"], art["num"], "",
             "", "", ccontent, ccontent, content_hash(ccontent), doc["publish_date"],
             doc["effective_date"], doc["issuing_authority"], status, doc["source_file"],
             doc["law_level"], doc["law_province"], doc["law_city"],),
        ).lastrowid
        article_ids.append(int(acid))
    # FTS5 是外部内容表，不会随 law_articles 自动更新，必须按 rowid 手工补写；
    # 触发器方案会额外改动现有表，这里保持最小侵入。
    conn.executemany(
        "INSERT INTO law_articles_fts(rowid, law_name, article, content, chapter, section) "
        "VALUES (?,?,?,?,?,?)",
        [(aid, doc["title"], art["content"], art["content"], "", "")
         for aid, art in zip(article_ids, doc["articles"])],
    )
    index_bigram_articles(conn, article_ids)
    return version_id, len(article_ids)


def rollback(conn: sqlite3.Connection, batch_id: str) -> dict:
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM law_versions WHERE batch_id=?", (batch_id,))]
    if not ids:
        return {"versions": 0, "articles": 0}
    marks = ",".join("?" * len(ids))
    arts = [r[0] for r in conn.execute(
        f"SELECT id FROM law_articles WHERE version_id IN ({marks})", ids)]
    removed_articles = len(arts)
    if arts:
        amarks = ",".join("?" * len(arts))
        conn.execute(f"DELETE FROM law_bigram_add WHERE article_id IN ({amarks})", arts)
        conn.execute(f"DELETE FROM law_articles WHERE id IN ({amarks})", arts)
        conn.execute(f"DELETE FROM law_versions WHERE id IN ({marks})", ids)
        # 外部内容表的删除走 rebuild 最稳（回滚是低频操作，慢一点可以接受）。
        conn.execute("INSERT INTO law_articles_fts(law_articles_fts) VALUES('rebuild')")
    else:
        conn.execute(f"DELETE FROM law_versions WHERE id IN ({marks})", ids)
    conn.execute(
        "UPDATE ingest_batches SET note = COALESCE(note,'') || ' | rolled back at ' || ? "
        "WHERE id=?", (now_iso(), batch_id))
    conn.commit()
    return {"versions": len(ids), "articles": removed_articles}


def main() -> None:
    parser = argparse.ArgumentParser(description="gjgzk 规章增量入库")
    parser.add_argument("--category", default="部门规章", choices=list(_CATEGORY_MAP))
    parser.add_argument("--group", default="", help="发布机关或省份，逗号分隔；空=全部")
    parser.add_argument("--doc-id", default="", help="只处理指定 doc_id，逗号分隔，供单点补录")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--batch-id", default="")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-nonumbered", action="store_true",
                        help="正文无条号的文书整篇存 1 条（条号记「全文」），默认不启用")
    parser.add_argument("--rollback", default="", help="按 batch_id 回滚")
    args = parser.parse_args()

    if args.rollback:
        conn = sqlite3.connect(args.db)
        ensure_schema(conn)
        print(json.dumps(rollback(conn, args.rollback), ensure_ascii=False))
        conn.close()
        return

    groups = [g.strip() for g in args.group.split(",") if g.strip()]
    doc_ids = {d.strip() for d in args.doc_id.split(",") if d.strip()}
    docs, parse_stats = collect_documents(args.category, groups, args.limit, doc_ids,
                                          allow_nonumbered=args.allow_nonumbered)
    doc_groups, dropped = plan_documents(docs)
    flat = [d for g in doc_groups for d in g]
    batch_id = args.batch_id or f"gjgzk-{args.category}-{now_iso()}"

    result = dict(parse_stats)
    result.update({"batch_id": batch_id, "category": args.category,
                   "group": groups, "parseable_docs": len(flat) + len(dropped),
                   "planned_versions": len(flat),
                   "dropped_same_version": len(dropped),
                   "articles": sum(len(d["articles"]) for d in flat),
                   "gap_docs": sum(1 for d in flat if d["gaps"]),
                   "missing_numbers": sum(len(d["gaps"]) for d in flat),
                   "dup_docs": sum(1 for d in flat if d["duplicates"]),
                   "docs_with_order_no": sum(1 for d in flat if d["order_no"]),
                   "docs_with_publish_date": sum(1 for d in flat if d["publish_date"]),
                   "docs_with_effective_date": sum(1 for d in flat if d["effective_date"]),
                   "nonumbered_docs": sum(1 for d in flat if d.get("nonumbered"))})
    if args.dry_run:
        # 试跑不碰库：schema 迁移和状态校正都会写数据，不预览。
        result["dry_run"] = True
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    conn = sqlite3.connect(args.db)
    ensure_schema(conn)
    started = now_iso()
    conn.execute("INSERT INTO ingest_batches(id, started_at, operator) VALUES (?,?,?)",
                 (batch_id, started, "ingest_gjgzk_rules"))
    added = skipped_conflict = 0
    added_articles = 0
    status_changes = 0
    conflict_samples: list[str] = []
    today = datetime.date.today().isoformat()
    for group_docs in doc_groups:
        inserted: list[tuple[dict, int, int]] = []
        for doc in group_docs:
            norm = norm_law_name(doc["title"])
            old = existing_version(conn, norm, doc["law_type"], batch_id)
            if old is not None:
                skipped_conflict += 1
                if len(conflict_samples) < 10:
                    conflict_samples.append(f"{doc['doc_id']}|{doc['title']}|existing={old[0]}")
                continue
            vid, n = insert_document(conn, doc, batch_id)
            inserted.append((doc, vid, n))
            added += 1
            added_articles += n
        # 同名多版本就地定链：新版未生效时旧版继续现行，新版生效后旧版降级。
        status_changes += resolve_version_chain(conn, inserted, today)
    conn.execute(
        "UPDATE ingest_batches SET finished_at=?, file_count=?, added_laws=?, added_articles=?, "
        "note=? WHERE id=?",
        (now_iso(), len(flat), added, added_articles,
         json.dumps({**result, "added_versions": added,
                     "skipped_conflict": skipped_conflict,
                     "status_changes": status_changes,
                     "dropped": dropped}, ensure_ascii=False),
         batch_id))
    conn.commit()
    result.update({"added_versions": added, "added_articles": added_articles,
                   "skipped_conflict": skipped_conflict,
                   "status_changes": status_changes,
                   "conflict_samples": conflict_samples,
                   "dropped": dropped})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    conn.close()


if __name__ == "__main__":
    main()
