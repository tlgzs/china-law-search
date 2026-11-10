# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""入库法名校验闸门。

md 的元数据法名与正文可能整篇错挂：元数据写《…节约能源法…办法》，正文
却是别的法（库内 version 2474 就是一个 md 拼了多部北京地方性法规）。这类
文件一旦入库，A 法的条文就挂在 B 法名下，律师按 B 法引用会引到完全无关的
条文——比查不到危险得多，而且检索侧无法察觉。

两道判据，任一不通过即整篇拒收：

  1. 标题判据：正文有可靠的一级标题时，与元数据法名比对，相似度 < 0.65
     且互不为前缀即拒收。前缀放行是为了放过「法名 + 公布日期括号」这种
     同一部法的写法差异。
  2. 夹带判据：正文段落起点出现独立成行的他法标题（形如《XX条例》，且
     下一段紧接着第X章/第X条），与元数据法名相似度 < 0.60 即拒收。一个
     文件拼了多部法时，第二部及以后的法名会命中这条。

只拒收不改名：改名要先判断是「同一法不同写法」还是「真错挂」，留给人工。
拒收不是丢弃，是等人工把拼接文件拆成单部法后分别入库。
"""

from __future__ import annotations

import difflib
import json
import os
import re
import time

# 法名与正文一级标题低于此相似度，判为不是同一部法
TITLE_THRESHOLD = 0.65
# 正文夹带的他法标题与元数据法名低于此相似度，判为拼接错挂
EMBEDDED_THRESHOLD = 0.60

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REJECT_LOG = os.path.join(_PROJECT_ROOT, "state", "rejected-sources.jsonl")

# 文件名文档号前缀会连进法名：gjgzk 的「l5726165_」、flk 的「02555_e7b640e17aa5_」。
# 不剥掉的话，与正文标题比对时相似度被压到 0.58–0.64，整部法误判为错挂。
_DOC_PREFIX = re.compile(r"^(?:[A-Za-z]?\d{4,}_[0-9a-fA-F]{6,}_|[A-Za-z]?\d{3,}_)")
# pandoc 装饰：{#id}、{.smallcaps}、[文字]{.smallcaps}
_PANDOC_DECO = re.compile(r"\{[^{}]*\}")
_PANDOC_LINK = re.compile(r"\[([^\]]*)\](?:\{[^}]*\}|\([^)]*\))")
# 结构标题不是法规名
_TITLE_SKIP = re.compile(r"^(目录|总则|附则|正文|说明)$")
# 法规名几乎都以这些词收尾
_TITLE_SUFFIX = re.compile(
    r"(条例|办法|规定|细则|规则|决定|决议|方案|章程|守则|纲要|标准|公约|法)$")
# 标题里带公布日期括号时，取不到纯法名，交给前缀判据处理
_DATE = re.compile(r"\d{4}年\d{1,2}月")
_ARTICLE = re.compile(r"^第[零〇一二三四五六七八九十百千万亿0-9]+条")
_STRUCT = re.compile(r"^第[零〇一二三四五六七八九十百千万亿0-9]+[编章节]")
# 独立标题里要有辖区词，避免把「一般规定」这类节标题当成法名
_JURISDICTION = re.compile(
    r"(省|市|自治区|自治州|自治县|自治旗|旗|中华人民共和国|国务院|特别行政区)")


def _strip_doc_prefix(name: str) -> str:
    """剥掉文件名文档号前缀，只留法规名本体。"""
    return _DOC_PREFIX.sub("", (name or "").strip(), count=1)


def clean_line(text: str) -> str:
    """去掉行首标记（#、>、*、_）与 pandoc 装饰，返回纯文本。"""
    s = _PANDOC_LINK.sub(r"\1", text or "")
    s = _PANDOC_DECO.sub("", s)
    s = re.sub(r"^[\s#*_>]+|[\s#*_]+$", "", s)
    return s.strip().rstrip("_＿").strip()


def _normalize(name: str) -> str:
    """只留文字，去掉书名号、括号与空白，供相似度比较。"""
    return re.sub(r"[《》〈〉（）()【】\[\]\s]", "", _strip_doc_prefix(name))


def is_law_title(text: str) -> bool:
    """判断一行是否像完整的法规标题（不带公布日期括号等附加信息）。"""
    key = re.sub(r"\s+", "", text or "")
    if not 4 <= len(key) <= 60:
        return False
    if _TITLE_SKIP.match(key):
        return False
    if key.startswith("（") or _DATE.search(key) or "://" in key:
        return False
    return bool(_TITLE_SUFFIX.search(key))


def find_embedded_titles(law_name: str, lines: list[str]) -> list[str]:
    """找正文段落起点独立成行的他法标题，用于识别一个文件拼了多部法。

    只在第一条法条之后找，要求整行就是标题（不带书名号、括号，排除句内
    引用），且下一段紧接着第X编/章/节。返回与元数据法名明显不同的标题。
    """
    target = _normalize(law_name)
    if not target:
        return []

    found: list[str] = []
    started = False
    for i, raw in enumerate(lines):
        line = clean_line(raw)
        if not line:
            continue
        if not started:
            if _ARTICLE.match(line):
                started = True
            continue
        if raw.strip().startswith("#") or _STRUCT.match(line) or line.startswith("第"):
            continue
        if i > 0 and lines[i - 1].strip():
            continue  # 不是段落起点，多半是上一条的续行
        if any(ch in line for ch in "《》（"):
            continue  # 句内引用他法
        if not is_law_title(line) or not _JURISDICTION.search(line):
            continue
        following = [clean_line(x) for x in lines[i + 1:i + 9] if x.strip()][:3]
        if not any(_STRUCT.match(x) for x in following):
            continue  # 后面没有第X章/节，不像新一部法的开头
        candidate = _normalize(line)
        if len(candidate) < 8:
            continue
        if candidate.startswith(target) or target.startswith(candidate):
            continue
        ratio = difflib.SequenceMatcher(None, target, candidate).ratio()
        if ratio < EMBEDDED_THRESHOLD:
            found.append(line)
    return found


def check_law_name(
    law_name: str,
    title: str,
    embedded_titles: list[str] | None = None,
) -> str | None:
    """校验元数据法名与正文是否对应。返回 None 表示通过，否则返回拒收原因。"""
    if not law_name:
        return None
    target = _normalize(law_name)
    if not target:
        return None

    if title:
        heading = _normalize(title)
        if heading and not (target.startswith(heading) or heading.startswith(target)):
            ratio = difflib.SequenceMatcher(None, target, heading).ratio()
            if ratio < TITLE_THRESHOLD:
                return (f"法名与正文标题不一致（相似度 {ratio:.2f}）："
                        f"元数据「{law_name}」/ 标题「{title}」")

    if embedded_titles:
        return f"正文夹带其他法规标题：{'、'.join(embedded_titles[:4])}"

    return None


def record_rejection(source_file: str, law_name: str, reason: str) -> None:
    """把拒收记录追加到 state/rejected-sources.jsonl，便于人工复核。"""
    try:
        os.makedirs(os.path.dirname(_REJECT_LOG), exist_ok=True)
        with open(_REJECT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "source_file": source_file,
                "law_name": law_name,
                "reason": reason,
                "action": "需人工拆分后分别入库",
            }, ensure_ascii=False) + "\n")
    except OSError:
        # 记录失败不能阻断入库：拒收本身已生效，日志只是复核线索
        pass
