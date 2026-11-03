# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""把国家法律法规数据库（flk.npc.gov.cn）「批量下载文件」得到的 docx 整理成 inbox Markdown。

为什么需要这个脚本：flk 的批量下载包文件名是 `法规名称_公布日期YYYYMMDD.docx`，
既没有施行日期也没有法规分类，而 `ingest/parse_markdown.py` 依赖
`{名称}_公布日期{YYYY-MM-DD}_施行日期{YYYY-MM-DD}_法律法规分类{分类}.md`
这种命名才能正确入库。直接丢进 inbox 会整批丢日期和分类。

数据来源说明：flk 的 robots.txt 全站 Disallow，本项目不做程序抓取。
这里的输入必须是**人工从 flk 网页上点「批量下载文件」得到的文件**。

用法：
  python tools/import_flk.py --src "下载的法条/2026年11月3日下载" --inbox inbox/flk
  python tools/update.py --inbox inbox/flk          # 出变更报告，人工看
  python tools/update.py --apply <plan.json> --approve

分类判定顺序：修改决定 → 司法解释 → 法律 → 行政法规 → 地方性法规。
判不准的会列进「分类存疑」清单，不静默猜 —— 分类错了会让筛选漏掉整部法。
"""
from __future__ import annotations

import argparse
import collections
import os
import re
import sys
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "..", "ingest"))

from fetch_mirror import (  # noqa: E402
    build_markdown,
    extract_effective_date,
    extract_publish_date,
    is_amendment,
    safe_name,
    to_markdown,
)

# 标签必须与 ingest/parse_markdown.py 的 _LAW_TYPE_MAP 一致，否则分类落成 other
T_LAW = ("law", "法律")
T_REGULATION = ("regulation", "行政法规")
T_LOCAL = ("local_regulation", "地方性法规")
T_JUDICIAL = ("judicial_interpretation", "司法解释")
T_AMEND = ("amendment", "修改决定")
T_CONSTITUTION = ("constitution", "宪法")

# 行政法规的典型末尾；「条例」但带地名的是地方性法规，不能只看后缀
_ADMIN_SUFFIX = ("条例", "实施细则", "实施办法", "规定", "办法")
_PLACE = re.compile(
    r"(省|市|县|区|自治区|自治州|自治县|盟|旗|镇|乡)"
)
_CN_DATE = re.compile(r"_(\d{4})(\d{2})(\d{2})\.docx$", re.I)

# flk 下载的 docx 只有正文，没有制发机关、没有国务院令号，有些法规靠标题和正文
# 都推不出层级 —— 例如「事业单位登记管理条例」通篇不出现「国务院」三个字，
# 但它替代的《事业单位登记管理暂行条例》是国务院行政法规。
# 这类判不准的显式登记在此，不靠猜：分类错了会让类型筛选整部漏掉。
_TYPE_OVERRIDES: dict[str, tuple[str, str]] = {
    "事业单位登记管理条例": T_REGULATION,
    # 海南自贸港法规由海南省人大及其常委会制定，属地方性法规
    "海南自由贸易港中医药发展促进条例": T_LOCAL,
    "海南自由贸易港休闲渔业促进规定": T_LOCAL,
    # 牡丹江市的地方性法规，标题不带行政区划
    "镜泊湖水域船舶管理条例": T_LOCAL,
}


def classify(title: str, text: str) -> tuple[tuple[str, str] | None, str]:
    """返回 (类型元组, 存疑原因)。类型为 None 表示判不准，交给人工。"""
    t = title.strip()

    if t in _TYPE_OVERRIDES:
        return _TYPE_OVERRIDES[t], ""

    if t.endswith("宪法") or t.endswith("宪法修正案"):
        return T_CONSTITUTION, ""
    if t.endswith("宪法（2018年修正文本）"):
        return T_CONSTITUTION, ""
    if is_amendment(t):
        return T_AMEND, ""
    if "最高人民法院" in t or "最高人民检察院" in t:
        return T_JUDICIAL, ""
    if t.startswith("国务院"):
        return T_REGULATION, ""

    if t.startswith("中华人民共和国"):
        # 冠「中华人民共和国」的，条约/条例类归行政法规，其余归法律
        if any(t.endswith(s) for s in _ADMIN_SUFFIX) or "实施条例" in t:
            return T_REGULATION, ""
        return T_LAW, ""

    # 没有冠名又不像地方法规的，多半是国务院令（如「事业单位登记管理条例」）。
    # 判据看正文有没有国务院令号或国务院签署。
    if not _PLACE.search(t) and any(t.endswith(s) for s in _ADMIN_SUFFIX):
        if "国务院" in text[:2000] or "国务院令" in text:
            return T_REGULATION, ""
        return None, "无地名、非冠名，正文未见国务院字样，无法判定是行政法规还是地方性法规"

    return T_LOCAL, ""


def main() -> None:
    ap = argparse.ArgumentParser(description="把 flk 批量下载的 docx 整理成 inbox Markdown")
    ap.add_argument("--src", required=True, help="flk 批量下载解压后的目录（可含子目录）")
    ap.add_argument("--inbox", default="inbox/flk", help="输出目录")
    ap.add_argument("--tmp", default="/tmp/flk-md", help="pandoc 中间文件目录")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.exists():
        raise SystemExit(f"目录不存在：{src}")

    out = Path(args.inbox)
    out.mkdir(parents=True, exist_ok=True)
    Path(args.tmp).mkdir(parents=True, exist_ok=True)

    # 同一个文件可能在多个批量包里重复出现，按（标题, 公布日）去重。
    # 标题必须从文件名精确切出：flk 的命名是「法规名称_YYYYMMDD.docx」，
    # 名称本身可能含下划线以外的符号，只能靠日期段的位置反推，不能用固定长度截。
    seen: dict[tuple[str, str], Path] = {}
    for p in sorted(src.rglob("*.docx")):
        if p.name.startswith("~$"):
            continue
        m = _CN_DATE.search(p.name)
        if m:
            title = p.name[: m.start()]
            key = (title, "".join(m.groups()))
        else:
            title = p.stem
            key = (title, "")
        seen.setdefault(key, p)

    ok = 0
    no_date: list[str] = []
    suspects: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    types: collections.Counter = collections.Counter()
    pending: list[tuple[str, str]] = []

    for (title, _), path in sorted(seen.items()):
        try:
            text = to_markdown(path, Path(args.tmp))
        except Exception as exc:  # noqa: BLE001
            failed.append((title, f"转换失败：{exc}"))
            continue

        # 公布日期优先用 flk 文件名里的，那是官方元数据；取不到再回正文
        m = _CN_DATE.search(path.name)
        if m:
            pub = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        else:
            pub = extract_publish_date(text)
        if not pub:
            no_date.append(title)
            continue

        eff = extract_effective_date(text)

        kind, why = classify(title, text)
        if kind is None:
            suspects.append((title, why))
            kind = T_LOCAL  # 先按地方性法规写出，人工核对后再改

        md = build_markdown(title, text, is_amendment(title))
        fname = (
            f"{safe_name(title)}_公布日期{pub}"
            f"_施行日期{eff or pub}_法律法规分类{kind[1]}.md"
        )
        (out / fname).write_text(md, encoding="utf-8")
        ok += 1
        types[kind[1]] += 1
        if eff and eff > "2026-11-03":
            pending.append((title, eff))

    print(f"写出 {ok} 份到 {out}")
    print("  分类分布：" + "／".join(f"{k} {v}" for k, v in types.most_common()))
    if pending:
        print(f"  其中 {len(pending)} 份施行日在今天之后（入库时会标 pending）：")
        for t, e in pending[:10]:
            print(f"    - {e} 起施行  {t[:44]}")
    if no_date:
        print(f"  [需人工] {len(no_date)} 份未取到公布日期，未写入：")
        for t in no_date[:20]:
            print("    -", t)
    if suspects:
        print(f"  [分类存疑] {len(suspects)} 份已按地方性法规写出，请核对：")
        for t, why in suspects:
            print(f"    - {t[:44]}｜{why}")
    if failed:
        print(f"  [失败] {len(failed)} 份：")
        for t, why in failed[:20]:
            print(f"    - {t[:44]}｜{why}")


if __name__ == "__main__":
    main()
