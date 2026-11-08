# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""从法规名称前缀推导省 / 市 / 层级，回填 law_versions / law_articles。

数据现实：库里没有省、市字段，也没有 `issuing_authority`（22,077 个版本为空），
唯一的地区线索就是法规名称前缀（「沈阳市院前医疗急救管理条例」→ 沈阳市）。
所以本工具的全部结论都来自名称前缀，推断不出的**留空并列入缺口清单**，不做猜测。

层级判定：
  law_type 不是地方法规        → national（中央立法）
  名称前缀是省 / 自治区        → province
  名称前缀是市 / 自治州 / 盟 / 地区 → city
  都不是                        → 留空，进缺口清单

用法：
  python tools/geo_backfill.py --db data/law_data.db --dry-run --csv reports/geo-mapping.csv
  python tools/geo_backfill.py --db data/law_data.db --apply
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "ingest"))

from schema import bump_corpus_version, ensure_schema, meta_get  # noqa: E402

# 34 个省级行政区（法定名称，非推测）。直辖市按「省 = 市」处理。
_PROVINCES = [
    "北京市", "天津市", "上海市", "重庆市",
    "河北省", "山西省", "辽宁省", "吉林省", "黑龙江省", "江苏省", "浙江省",
    "安徽省", "福建省", "江西省", "山东省", "河南省", "湖北省", "湖南省",
    "广东省", "海南省", "四川省", "贵州省", "云南省", "陕西省", "甘肃省",
    "青海省", "台湾省", "内蒙古自治区", "广西壮族自治区", "西藏自治区",
    "宁夏回族自治区", "新疆维吾尔自治区", "香港特别行政区", "澳门特别行政区",
]
_MUNICIPALITIES = {"北京市", "天津市", "上海市", "重庆市"}

# 名称前缀不是行政区的常见词，避免把「城市市容」「商品交易市场」当成市名
_CITY_BLACKLIST_PREFIX = ("城市", "市场", "都市", "集市", "城乡", "城镇", "全国",
                          "关于", "实施", "促进", "不设", "人民", "中华",
                          "劳动", "体育")
# 出现在市名里说明它不是地名
_CITY_BLACKLIST_SUB = ("关于", "实施", "促进", "不设", "中华人民共和国", "劳动力",
                       "自由贸易港", "自治县", "自治旗")

# 自贸港、经济特区等特殊写法归到省级
_ALIAS = {
    "海南自由贸易港": {"level": "province", "province": "海南省", "city": "海南省",
                       "basis": "自由贸易港（省级）"},
}

# 「XX经济特区」在法规名里很常见（深圳、珠海、厦门、汕头、海南），归一为「XX市」
_SPECIAL_ZONE = re.compile(r"\A([\u4e00-\u9fa5]{2,6})经济特区")

_CITY_RE = re.compile(r"\A([\u4e00-\u9fa5]{2,8}?(?:市|自治州|盟|地区))")
_COUNTY_RE = re.compile(r"(?:县|旗|区|镇)$")


def province_of(name: str) -> str:
    for p in _PROVINCES:
        if name.startswith(p):
            return p
    return ""


def city_token(name: str) -> str:
    """从名称前缀取行政区名；取不到或明显是普通词时返回空串。"""
    for alias, info in _ALIAS.items():
        if name.startswith(alias):
            return ""
    m = _SPECIAL_ZONE.match(name)
    if m:
        return m.group(1) + "市"
    m = _CITY_RE.match(name)
    if not m:
        return ""
    token = m.group(1)
    if any(token.startswith(b) for b in _CITY_BLACKLIST_PREFIX):
        return ""
    if any(b in token for b in _CITY_BLACKLIST_SUB):
        return ""
    # 「杭州城市总体规划…」里的「杭州城市」不是市名：去掉尾巴再拼成「杭州市」
    if token.endswith("城市"):
        core = token[:-2]
        if len(core) < 2 or _COUNTY_RE.search(core):
            return ""
        token = core + "市"
    if _COUNTY_RE.search(token):
        return ""
    return token


def classify(law_name: str, law_type: str) -> dict:
    """返回 {level, province, city, basis}。"""
    name = (law_name or "").replace("《", "").replace("》", "").strip()
    for alias, info in _ALIAS.items():
        if name.startswith(alias):
            return dict(info)
    if law_type != "local_regulation":
        return {"level": "national", "province": "", "city": "",
                "basis": "law_type 非地方法规"}
    prov = province_of(name)
    if prov:
        if prov in _MUNICIPALITIES:
            return {"level": "province", "province": prov, "city": prov,
                    "basis": "省级行政区前缀（直辖市）"}
        # 「内蒙古自治区呼和浩特市…」这类写法：省属市，能同时拿到省与市
        rest_city = city_token(name[len(prov):])
        if rest_city:
            return {"level": "city", "province": prov, "city": rest_city,
                    "basis": "省级前缀 + 市名前缀"}
        return {"level": "province", "province": prov, "city": "",
                "basis": "省级行政区前缀"}
    city = city_token(name)
    if city:
        return {"level": "city", "province": "", "city": city,
                "basis": "行政区名前缀（省属关系需外部行政区划表，缺口）"}
    return {"level": "", "province": "", "city": "", "basis": "无法从前缀推断（缺口）"}


def build_mapping(conn: sqlite3.Connection, law_type: str = "local_regulation") -> dict:
    rows = conn.execute(
        "SELECT law_name, COUNT(*) FROM law_versions WHERE law_type=? GROUP BY law_name",
        (law_type,)).fetchall()
    stats = Counter()
    cities = Counter()
    provinces = Counter()
    gaps: list[tuple[str, int]] = []
    for law_name, n in rows:
        c = classify(law_name, law_type)
        stats[c["level"] or "unknown"] += 1
        if c["city"]:
            cities[c["city"]] += 1
        if c["province"]:
            provinces[c["province"]] += n
        if not c["level"]:
            gaps.append((law_name, n))
    zhou = [c for c in cities if c.endswith(("自治州", "盟", "地区"))]
    shi = [c for c in cities if c.endswith("市")]
    return {"laws": len(rows), "levels": dict(stats), "cities": cities, "provinces": provinces,
            "city_count": len(cities), "city_shi": len(shi), "city_zhou": len(zhou),
            "gaps": gaps}


def apply_geo(conn: sqlite3.Connection, dry_run: bool) -> dict:
    counts = Counter()
    try:
        for row in conn.execute("SELECT id, law_name, law_type FROM law_versions"):
            c = classify(row["law_name"], row["law_type"])
            counts[c["level"] or "unknown"] += 1
            if dry_run:
                continue
            conn.execute(
                "UPDATE law_versions SET law_level=?, law_province=?, law_city=? WHERE id=?",
                (c["level"], c["province"], c["city"], row["id"]))
        if not dry_run:
            # 图形上万行，用一条 JOIN 回填，避免逐条 UPDATE 百万次
            conn.execute(
                """UPDATE law_articles SET
                     law_level   = (SELECT v.law_level   FROM law_versions v WHERE v.id = law_articles.version_id),
                     law_province= (SELECT v.law_province FROM law_versions v WHERE v.id = law_articles.version_id),
                     law_city    = (SELECT v.law_city    FROM law_versions v WHERE v.id = law_articles.version_id)
                   WHERE version_id IN (SELECT id FROM law_versions)""")
            counts["version"] = bump_corpus_version(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return dict(counts)


def write_csv(path: str, mapping: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["city", "law_count", "kind", "province", "confidence", "note"])
        for city, n in mapping["cities"].most_common():
            kind = "自治州/盟/地区" if city.endswith(("自治州", "盟", "地区")) else "设区的市"
            w.writerow([city, n, kind, "", "derived",
                        "省属关系无法从名称推导，缺口"])
        for law_name, n in mapping["gaps"][:50]:
            w.writerow(["", n, "未推断", "", "gap", law_name])


def main() -> None:
    ap = argparse.ArgumentParser(description="省市层级字段推导与回填")
    ap.add_argument("--db", default="./data/law_data.db")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--csv", default=None)
    ap.add_argument("--json", default=None)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    mapping = build_mapping(conn)
    print(f"地方性法规 {mapping['laws']} 部；层级分布 {mapping['levels']}")
    print(f"市/州/盟/地区名 {mapping['city_count']} 个（其中「市」{mapping['city_shi']} 个，"
          f"自治州/盟/地区 {mapping['city_zhou']} 个）")
    print(f"推断不出的法规 {len(mapping['gaps'])} 部")
    if args.csv:
        write_csv(args.csv, mapping)
        print(f"映射表已写入 {args.csv}")

    result = apply_geo(conn, dry_run=not args.apply)
    print(f"回填{'（已写库）' if args.apply else '（干跑）'}：{result}")
    print(f"corpus_version = {meta_get(conn, 'corpus_version', '0')}")
    conn.close()

    if args.json:
        payload = {k: (dict(v) if isinstance(v, Counter) else v) for k, v in mapping.items()}
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        print(f"统计已写入 {args.json}")


if __name__ == "__main__":
    main()
