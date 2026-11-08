#!/usr/bin/env python3
# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""把民政部行政区划代码整理成「市名 → 省 / 标准代码」可复核清单。

数据来源（离线快照，不联网）：
  reports/raw/division-codes-2023.txt
  原始出处：民政部《2023年中华人民共和国县以上行政区划代码》
  https://www.mca.gov.cn/mzsj/xzqh/2023/202301xzqh.html

输出：
  reports/division-cities.csv      市名 → 省 / 代码 / 类型
  reports/division-provinces.csv   省级行政区清单

类型取值：
  直辖市 / 设区的市 / 不设区的市 / 自治州 / 地区 / 盟 / 县级市

口径说明：
  - 「设区的市」指地级市中下辖市辖区（代码 XXYY01 起的「…区」）的那些；
    东莞、中山、嘉峪关、三沙、儋州这类不设区的市单列，刑法上的地方立法权口径不同。
  - 县级市（辛集市、义乌市等）也收进来，但标记为「县级市」，
    方便归一脚本把它们识别为「不属于设区的市」而不是当成没匹配上。
"""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = BASE_DIR / "reports" / "raw" / "division-codes-2023.txt"
DEFAULT_CITY_OUT = BASE_DIR / "reports" / "division-cities.csv"
DEFAULT_PROVINCE_OUT = BASE_DIR / "reports" / "division-provinces.csv"

MUNICIPALITY_CODES = {"110000", "120000", "310000", "500000"}

# 民政部 2023 代码表未列、但国务院已批准设立市辖区的补充项（有据可查，非推测）：
# 三沙市西沙区、南沙区，2020-04-18 民政部公告
# https://www.gov.cn/zhengce/zhengceku/2020-04/19/content_5504215.htm
DISTRICT_SUPPLEMENTS = {
    "460300": "西沙区/南沙区（2020-04-18 民政部公告设立，民政部 2023 代码表未列）",
}


def load_codes(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 2 or not re.fullmatch(r"\d{6}", parts[0]):
            continue
        rows.append((parts[0], parts[1].strip()))
    return rows


def build(rows: list[tuple[str, str]]) -> tuple[list[dict], list[dict]]:
    provinces = [(c, n) for c, n in rows if c[2:] == "0000"]
    # 省级代码 → 名称，供地级行回填省份
    province_of = {c[:2]: n for c, n in provinces}

    child_names: dict[str, list[str]] = {}
    for code, name in rows:
        if code[4:] != "00":
            child_names.setdefault(code[:4], []).append(name)

    cities: list[dict] = []
    for code, name in rows:
        prov = province_of.get(code[:2], "")
        if code[2:] == "0000":
            if code in MUNICIPALITY_CODES:
                cities.append({"code": code, "name": name, "province": name,
                               "province_code": code, "kind": "直辖市"})
            continue
        if "直辖县级行政区划" in name:
            continue
        if code[4:] == "00":
            if name.endswith("自治州"):
                kind = "自治州"
            elif name.endswith("地区"):
                kind = "地区"
            elif name.endswith("盟"):
                kind = "盟"
            elif any(n.endswith("区") for n in child_names.get(code[:4], [])) or code in DISTRICT_SUPPLEMENTS:
                kind = "设区的市"
            else:
                kind = "不设区的市"
            cities.append({"code": code, "name": name, "province": prov,
                           "province_code": code[:2] + "0000", "kind": kind,
                           "note": DISTRICT_SUPPLEMENTS.get(code, "")})
        elif name.endswith("市"):
            cities.append({"code": code, "name": name, "province": prov,
                           "province_code": code[:2] + "0000", "kind": "县级市"})

    province_rows = [{"code": c, "name": n} for c, n in provinces]
    return cities, province_rows


def main() -> None:
    ap = argparse.ArgumentParser(description="生成行政区划可复核清单")
    ap.add_argument("--source", default=str(DEFAULT_SOURCE))
    ap.add_argument("--city-out", default=str(DEFAULT_CITY_OUT))
    ap.add_argument("--province-out", default=str(DEFAULT_PROVINCE_OUT))
    args = ap.parse_args()

    rows = load_codes(Path(args.source))
    cities, provinces = build(rows)

    Path(args.city_out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.city_out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["code", "name", "province", "province_code", "kind", "note"])
        w.writeheader()
        w.writerows(cities)
    with open(args.province_out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["code", "name"])
        w.writeheader()
        w.writerows(provinces)

    from collections import Counter
    counts = Counter(c["kind"] for c in cities)
    print(f"源行数 {len(rows)}；省级 {len(provinces)}；市/州/盟/地区 {len(cities)}")
    for k, v in counts.most_common():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
