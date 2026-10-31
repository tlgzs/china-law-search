# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟

"""
法律法规 Markdown 解析器
========================
解析 flk.npc.gov.cn 下载的 Markdown 格式法规文件，
提取每一条法条的结构化数据。

文件名格式:
  {法规名称}_公布日期{YYYY-MM-DD}_施行日期{YYYY-MM-DD}_法律法规分类{分类}.md

文件内容结构:
  # 法规名称
  - 法律名称：xxx
  - 公布日期：xxx
  - 施行日期：xxx
  - 法律法规分类：xxx
  ---
  ## 第X编/章
  ### 第X节
  第X条  法条正文...

输出: 每条法条一个 dict，包含完整 metadata
"""

import os
import re
import json
from typing import Optional
from dataclasses import dataclass, field, asdict


# ============================================================
# 中文数字转阿拉伯数字
# ============================================================

_CN_NUM = {
    "零": 0, "〇": 0, "0": 0,
    "一": 1, "壹": 1, "1": 1,
    "二": 2, "贰": 2, "两": 2, "2": 2,
    "三": 3, "叁": 3, "3": 3,
    "四": 4, "肆": 4, "4": 4,
    "五": 5, "伍": 5, "5": 5,
    "六": 6, "陆": 6, "6": 6,
    "七": 7, "柒": 7, "7": 7,
    "八": 8, "捌": 8, "8": 8,
    "九": 9, "玖": 9, "9": 9,
}

_CN_UNIT = {
    "十": 10, "拾": 10,
    "百": 100, "佰": 100,
    "千": 1000, "仟": 1000,
    "万": 10000, "萬": 10000,
    "亿": 100000000, "億": 100000000,
}


def cn2num(cn: str) -> int:
    """中文数字转阿拉伯数字。如 '六百八十' -> 680, '一千二百六十' -> 1260"""
    if not cn:
        return 0
    # 纯阿拉伯数字直接返回
    if cn.isdigit():
        return int(cn)

    result = 0
    current = 0  # 当前段的值
    section = 0   # 万/亿分段前的值

    for char in cn:
        if char in _CN_NUM:
            current = current * 10 + _CN_NUM[char] if current >= 10 else _CN_NUM[char]
            # 处理 "十" 开头的情况, 如 "十一" -> 11
            if char == "一" and current == 1:
                pass  # 会被后续unit处理
        elif char in _CN_UNIT:
            unit = _CN_UNIT[char]
            if unit >= 10000:  # 万/亿: 分段
                section = (section + current) * unit
                current = 0
            else:  # 十/百/千
                if current == 0:
                    current = 1  # "十" 前面省略了 "一"
                section += current * unit
                current = 0
        else:
            # 遇到非数字字符,停止
            break

    return section + current


# ============================================================
# 文件名 metadata 提取
# ============================================================

# 标准格式: _公布日期2020-05-28_施行日期2021-01-01_法律法规分类法律.md
_FILENAME_PATTERNS = [
    re.compile(r"公布日期(\d{4}-\d{2}-\d{2})"),
    re.compile(r"施行日期(\d{4}-\d{2}-\d{2})"),
    re.compile(r"法律法规分类(.+?)\.md$"),
]

# 地方法规简短格式: _20200723.docx (8位日期)
_SHORT_DATE_PATTERN = re.compile(r"_(\d{4})(\d{2})(\d{2})\.(?:docx?|DOCX?|md)$")

# 文件名中分类名到 law_type 的映射
_LAW_TYPE_MAP = {
    "法律": ("law", "法律"),
    "行政法规": ("regulation", "行政法规"),
    "地方性法规": ("local_regulation", "地方性法规"),
    "地方法规": ("local_regulation", "地方性法规"),
    "司法解释": ("judicial_interpretation", "司法解释"),
    "监察法规": ("supervision_regulation", "监察法规"),
    "宪法": ("constitution", "宪法"),
    # 「关于修改《X》的决定」这类文件不是法典，是修改决定。混进「法律」会让
    # 检索《律师法》时同时命中决定正文，且决定正文没有「第X条」结构。
    "修改决定": ("amendment", "修改决定"),
}


def parse_filename(filename: str, dir_label: str = "") -> dict:
    """
    从文件名提取 metadata
    
    支持两种格式:
    1. 标准格式: {法规名称}_公布日期2020-05-28_施行日期2021-01-01_法律法规分类法律.md
    2. 地方简短格式: {法规名称}_20200723.docx
    """
    result = {
        "law_name": "",
        "publish_date": "",
        "effective_date": "",
        "law_type": "",
        "law_type_label": "",
    }

    # 尝试标准格式
    m = _FILENAME_PATTERNS[0].search(filename)
    if m:
        result["publish_date"] = m.group(1)

    m = _FILENAME_PATTERNS[1].search(filename)
    if m:
        result["effective_date"] = m.group(1)

    m = _FILENAME_PATTERNS[2].search(filename)
    if m:
        label = m.group(1).strip()
        result["law_type_label"] = label
        result["law_type"] = _LAW_TYPE_MAP.get(label, ("other", label))[0]

    # 如果标准格式没提取到日期,尝试简短格式
    if not result["publish_date"]:
        m = _SHORT_DATE_PATTERN.search(filename)
        if m:
            date_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            result["publish_date"] = date_str
            result["effective_date"] = date_str  # 地方简短格式只有一个日期

    # 如果没有从文件名提取到分类,用目录标签推断
    if not result["law_type"] and dir_label:
        # 目录名形如「地方法规_markdown」，而 _LAW_TYPE_MAP 的键是「地方法规」；
        # 不归一的话，凡文件名里没写分类的文件都会得到空分类。
        dir_key = re.sub(r"_markdown$", "", dir_label.strip())
        type_info = _LAW_TYPE_MAP.get(dir_key) or _LAW_TYPE_MAP.get(dir_label)
        if type_info:
            result["law_type"] = type_info[0]
            result["law_type_label"] = type_info[1]

    # 提取法规名称
    # 优先从标准格式提取
    name_end = filename.find("_公布日期")
    if name_end == -1:
        name_end = filename.find("_施行日期")
    if name_end == -1:
        name_end = filename.find("_法律法规分类")
    if name_end == -1:
        # 尝试简短日期格式
        m = _SHORT_DATE_PATTERN.search(filename)
        if m:
            name_end = m.start()
        else:
            # 没有日期,去掉扩展名
            name_end = filename.rfind(".") if "." in filename else len(filename)

    result["law_name"] = filename[:name_end].strip()
    # 清理文件名开头的特殊字符
    result["law_name"] = result["law_name"].lstrip("+*·")
    # 源文件名尾部常带 "_"（如「XX条例_.md」），会把同一部法拆成两个名称
    result["law_name"] = result["law_name"].rstrip("_＿").strip()

    return result


# ============================================================
# 法条切分
# ============================================================

# 匹配法条行: "第一条" / "第1条" / "第六百八十条"
_ARTICLE_PATTERN = re.compile(r"^第([零〇一二三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟0-9]+)条\s*")

# 匹配章标题: "## 第一章" / "第一章 总则" (纯文本也支持)
# 必须不包含"条"字,避免误匹配法条引用
_CHAPTER_PATTERN = re.compile(r"^#{0,4}\s*(第[零〇一二三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟0-9]+[编章部分])\s*(.*)")

# 匹配节标题: "### 第一节" / "第一节 借款合同" (纯文本也支持)
_SECTION_PATTERN = re.compile(r"^#{0,4}\s*(第[零〇一二三四五六七八九十百千万亿壹贰叁肆伍陆柒捌玖拾佰仟0-9]+节)\s*(.*)")

# 匹配文件头部的 metadata 行: "- 法律名称：xxx"
# 值必须允许为空：源文件里 "- 公布日期：" 常常是空值，若要求 (.+) 则整行不匹配，
# 解析器会误判 metadata 区结束，把其后的 "- 法律法规分类：地方性法规" 一并丢掉。
_META_PATTERN = re.compile(r"^[-*]\s*(.+?)[:：]\s*(.*)$")

# 引用块里的中文日期，如「（2026年8月28日第十四届全国人大常委会第二十四次会议通过）」
_CN_DATE_PATTERN = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")


def _publish_date_from_quotes(lines: list[str], limit: int = 40) -> str:
    """从文件头部引用块提取公布日期，取最晚的一个（沿革按时间递增）。

    pandoc 转出的法规文件把颁布信息放在「>」引用块里。解析主循环整行跳过引用块，
    文件名又没带日期时，公布日期就会整年为空的——这会让「按公布日期判断新旧版本」
    直接失效。
    """
    dates: list[str] = []
    for raw in lines[:limit]:
        s = raw.strip()
        if re.fullmatch(r"目\s*录", s.replace("\u3000", "")):
            break
        if not s.startswith(">"):
            continue
        for m in _CN_DATE_PATTERN.finditer(s):
            y, mo, d = m.groups()
            dates.append(f"{int(y):04d}-{int(mo):02d}-{int(d):02d}")
    return max(dates) if dates else ""


@dataclass
class Article:
    """单条法条"""
    law_name: str = ""
    law_type: str = ""
    law_type_label: str = ""
    article_number: str = ""        # "第六百八十条"
    article_number_num: int = 0     # 680
    chapter: str = ""               # "第十二章 借款合同"
    section: str = ""               # "第一节 借款合同"
    content: str = ""               # 法条全文
    publish_date: str = ""
    effective_date: str = ""
    issuing_authority: str = ""     # 制定机关
    status: str = "effective"       # 现行有效
    source_file: str = ""           # 来源文件名


@dataclass
class ParseResult:
    """单个文件解析结果"""
    law_name: str = ""
    law_type: str = ""
    law_type_label: str = ""
    publish_date: str = ""
    effective_date: str = ""
    issuing_authority: str = ""
    article_count: int = 0
    articles: list = field(default_factory=list)
    source_file: str = ""
    parse_errors: list = field(default_factory=list)


def parse_markdown_file(filepath: str, dir_label: str = "") -> ParseResult:
    """
    解析单个 Markdown 法规文件
    
    支持三种格式:
    1. 标准格式 (有 metadata 头 + ## 章节标题)
    2. pandoc 输出 (无 metadata 头, 纯文本章节标题, 有 > 引用块)
    3. textutil 输出 (纯文本, 无任何标记)
    
    Args:
        filepath: 文件路径
        dir_label: 目录标签 (用于推断 law_type, 如 "法律" "地方法规")
    
    Returns:
        ParseResult: 包含所有法条和 metadata
    """
    result = ParseResult()
    filename = os.path.basename(filepath)
    result.source_file = filename

    # 1. 从文件名提取 metadata
    fn_meta = parse_filename(filename, dir_label)
    result.law_name = fn_meta["law_name"]
    result.law_type = fn_meta["law_type"]
    result.law_type_label = fn_meta["law_type_label"]
    result.publish_date = fn_meta["publish_date"]
    result.effective_date = fn_meta["effective_date"]

    # 2. 读取文件内容
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        try:
            with open(filepath, "r", encoding="gbk") as f:
                lines = f.readlines()
        except Exception as e:
            result.parse_errors.append(f"文件编码错误: {e}")
            return result

    # 3. 解析文件内容
    current_chapter = ""
    current_section = ""
    in_metadata_block = True
    found_first_content = False  # 是否已找到第一条法条或章节标题

    # 文件名没带公布日期时，从头部引用块兜底（引用块在主循环里会被整行跳过）
    if not result.publish_date:
        result.publish_date = _publish_date_from_quotes(lines)

    for line in lines:
        line = line.rstrip("\n\r")
        stripped = line.strip()

        # 跳过空行
        if not stripped:
            continue

        # 跳过引用块 (> 开头, 通常是颁布信息)
        if stripped.startswith(">"):
            continue

        # 跳过 YAML front matter (--- ... ---)
        if stripped == "---":
            in_metadata_block = not in_metadata_block
            continue

        # 文件标题: "# 法规名称"
        if stripped.startswith("# ") and not stripped.startswith("## "):
            title = stripped[2:].strip().rstrip("_＿").strip()
            if title and not result.law_name:
                result.law_name = title
            in_metadata_block = False
            continue

        # metadata 行: "- 法律名称：xxx"
        if in_metadata_block:
            meta_match = _META_PATTERN.match(stripped)
            if meta_match:
                key = meta_match.group(1).strip()
                value = meta_match.group(2).strip()
                if "名称" in key and value:
                    result.law_name = value.rstrip("_＿").strip()
                elif "公布" in key and value:
                    result.publish_date = value
                elif "施行" in key and value:
                    result.effective_date = value
                elif "分类" in key and value:
                    result.law_type_label = value
                    result.law_type = _LAW_TYPE_MAP.get(
                        value, ("other", value)
                    )[0]
                elif "制定机关" in key or "公布机关" in key:
                    result.issuing_authority = value
                continue
            # 非 metadata 格式的行, 退出 metadata 区
            in_metadata_block = False

        # 章标题: ## 第X章 / 第X编 / 第X部分 (纯文本也支持)
        # 必须不包含"条"字, 避免误匹配法条引用
        chapter_match = _CHAPTER_PATTERN.match(stripped)
        if chapter_match and "条" not in stripped:
            current_chapter = stripped.lstrip("#").strip()
            # 换章必须清空节：文件开头的「目录」会把最后一个节（如「第三节 非全日制用工」）
            # 提前写进 current_section，若不清空，全文每一条都会挂上那个节。
            # 且「节」只在所属章内有效，跨章继承本身就是错的。
            current_section = ""
            found_first_content = True
            continue

        # 节标题: ### 第X节 / 第X节 (纯文本也支持)
        section_match = _SECTION_PATTERN.match(stripped)
        if section_match and "条" not in stripped:
            current_section = stripped.lstrip("#").strip()
            found_first_content = True
            continue

        # 其他标题行 (跳过, 不作为法条)
        if stripped.startswith("#"):
            continue

        # 法条行: 第X条 ...
        article_match = _ARTICLE_PATTERN.match(stripped)
        if article_match:
            article_num_cn = article_match.group(1)
            article_num = cn2num(article_num_cn)

            # 法条正文 (去掉 "第X条" 前缀)
            content = _ARTICLE_PATTERN.sub("", stripped).strip()

            # 处理全角空格和制表符
            content = content.replace("\u3000", "").replace("\t", "").strip()

            # 如果正文为空,可能法条内容在下一行
            if not content:
                continue

            # 如果法规名称为空, 尝试从内容推断
            if not result.law_name and not found_first_content:
                # 法条之前的纯文本行可能是法规名称
                pass

            article = Article(
                law_name=result.law_name,
                law_type=result.law_type,
                law_type_label=result.law_type_label,
                article_number=f"第{article_num_cn}条",
                article_number_num=article_num,
                chapter=current_chapter,
                section=current_section,
                content=content,
                publish_date=result.publish_date,
                effective_date=result.effective_date,
                issuing_authority=result.issuing_authority,
                status="effective",
                source_file=filename,
            )
            result.articles.append(article)
            found_first_content = True
            continue

        # 如果法条之前的纯文本行且法规名称为空, 作为法规名称
        if not found_first_content and not result.law_name and len(stripped) < 100:
            # 检查是否像法规名称 (不含数字开头, 不太长)
            if not stripped[0].isdigit() and len(stripped) < 50:
                result.law_name = stripped
                continue

        # 如果已有法条, 追加到上一条法条的续行
        if result.articles:
            last_article = result.articles[-1]
            last_article.content += "\n" + stripped

    # 清理法条正文 (去除多余空行)
    for article in result.articles:
        article.content = re.sub(r"\n{3,}", "\n\n", article.content).strip()

    result.article_count = len(result.articles)
    return result


# ============================================================
# 批量解析
# ============================================================

def parse_directory(
    input_dir: str,
    output_file: str = None,
    progress_every: int = 100,
    dir_label: str = "",
) -> dict:
    """
    批量解析目录下所有 Markdown 文件
    
    Args:
        input_dir: Markdown 文件所在目录
        output_file: 输出 JSON 文件路径 (None=不写文件)
        progress_every: 每处理多少个文件打印一次进度
        dir_label: 目录标签 (用于推断 law_type)
    
    Returns:
        dict: 统计信息 {total_files, total_articles, errors, by_type}
    """
    all_articles = []
    stats = {
        "total_files": 0,
        "total_articles": 0,
        "errors": [],
        "by_type": {},
        "by_law": {},
    }

    # 收集所有 .md 文件
    md_files = []
    for root, dirs, files in os.walk(input_dir):
        for f in files:
            if f.endswith(".md"):
                md_files.append(os.path.join(root, f))

    md_files.sort()
    stats["total_files"] = len(md_files)

    for i, filepath in enumerate(md_files):
        if (i + 1) % progress_every == 0:
            print(f"  进度: {i+1}/{len(md_files)} 文件, "
                  f"已提取 {len(all_articles)} 条法条")

        result = parse_markdown_file(filepath, dir_label=dir_label)

        if result.parse_errors:
            stats["errors"].extend(
                [{"file": result.source_file, "error": e}
                 for e in result.parse_errors]
            )

        for article in result.articles:
            article_dict = asdict(article)
            all_articles.append(article_dict)

        # 统计
        ltype = result.law_type or "unknown"
        stats["by_type"][ltype] = stats["by_type"].get(ltype, 0) + 1

        lname = result.law_name or "unknown"
        stats["by_law"][lname] = stats["by_law"].get(lname, 0) + result.article_count

    stats["total_articles"] = len(all_articles)

    # 写输出文件
    if output_file:
        os.makedirs(os.path.dirname(output_file), exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(all_articles, f, ensure_ascii=False, indent=2)
        print(f"\n已写入 {len(all_articles)} 条法条到 {output_file}")

    return stats


# ============================================================
# 命令行入口
# ============================================================

if __name__ == "__main__":
    import sys

    # 默认路径 (相对于项目根目录)
    BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)
    ))))
    
    # 法律法规库根目录 (从项目根目录向上找)
    LAW_BASE = os.path.join(os.path.dirname(BASE), "法律法规库")
    if not os.path.exists(LAW_BASE):
        # 尝试相对于当前工作目录
        LAW_BASE = os.path.join(os.getcwd(), "法律法规库")

    # 要解析的 markdown 目录
    md_dirs = [
        ("法律", os.path.join(LAW_BASE, "法律_markdown")),
        ("行政法规", os.path.join(LAW_BASE, "行政法规_markdown")),
        ("司法解释", os.path.join(LAW_BASE, "司法解释_markdown")),
        ("监察法规", os.path.join(LAW_BASE, "监察法规_markdown")),
        ("地方法规", os.path.join(LAW_BASE, "地方法规_markdown")),
        ("宪法", os.path.join(LAW_BASE, "宪法_markdown")),
    ]

    output_file = os.path.join(os.path.dirname(__file__), "parsed_articles.json")

    all_stats = {}
    total_articles = 0

    for label, md_dir in md_dirs:
        if not os.path.exists(md_dir):
            print(f"\n[跳过] {label}: 目录不存在 ({md_dir})")
            continue

        print(f"\n{'='*60}")
        print(f"解析 {label}: {md_dir}")
        print(f"{'='*60}")

        stats = parse_directory(md_dir, output_file=None, progress_every=200)
        all_stats[label] = stats
        total_articles += stats["total_articles"]

        print(f"  文件数: {stats['total_files']}")
        print(f"  法条数: {stats['total_articles']}")
        if stats["errors"]:
            print(f"  错误数: {len(stats['errors'])}")

    # 汇总写入
    print(f"\n{'='*60}")
    print(f"全部完成! 总法条数: {total_articles}")
    print(f"{'='*60}")

    # 写汇总 JSON
    all_articles = []
    for label, md_dir in md_dirs:
        if not os.path.exists(md_dir):
            continue
        for root, dirs, files in os.walk(md_dir):
            for f in files:
                if f.endswith(".md"):
                    result = parse_markdown_file(os.path.join(root, f))
                    for article in result.articles:
                        all_articles.append(asdict(article))

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(all_articles, f, ensure_ascii=False, indent=2)

    print(f"全部法条已写入: {output_file}")
    print(f"总计: {len(all_articles)} 条法条")
