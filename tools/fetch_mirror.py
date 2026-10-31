# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""把 lawtext/legal-data-docx 镜像里更新的法规整理成 inbox 可用的 Markdown。

链路：镜像索引 JSONL → 本地原件 → pandoc/textutil 转 Markdown → 提取公布/施行日期
     → 按 flk 命名规则写入 inbox/mirror/ → 交给 tools/update.py 出变更报告。

**只做「整理成 inbox」这一步，不写数据库。** 写库统一走 update.py 的人工闸门。

用法：
  # 1) 拉原件（已下载到本地时跳过）
  python tools/fetch_mirror.py --index /tmp/lawtext/data-docx.jsonl \\
      --raw /tmp/lawtext/raw --map /tmp/lawtext/pull-map.json --inbox inbox/mirror

  # 2) 出变更报告，人工看
  python tools/update.py --inbox inbox/mirror

命名规则（与 ingest/parse_markdown.py 的 parse_filename 对齐）：
  {法规名称}_公布日期YYYY-MM-DD_施行日期YYYY-MM-DD_法律法规分类{分类}.md

修改决定的特殊处理：
  决定正文是「一、将第X条…修改为…」，不含「第X条」开头的行，通用解析器会切出 0 条。
  这里把它压成一条「第1条　（全文）」+ 后续所有行作为续行，保证全文可被 FTS 命中，
  不伪造项号、也不与母法条号混淆。
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "ingest"))

# 目录前缀 → (law_type, law_type_label)
PATH_TYPE = {
    "01": ("constitution", "宪法"),
    "02": ("law", "法律"),
    "03": ("regulation", "行政法规"),
    "04": ("supervision_regulation", "监察法规"),
    "05": ("judicial_interpretation", "司法解释"),
}
LOCAL_TYPE = ("local_regulation", "地方性法规")
AMEND_TYPE = ("amendment", "修改决定")

_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_EFFECTIVE_RE = re.compile(r"自(\d{4})年(\d{1,2})月(\d{1,2})日起(?:施行|实施|生效)")
_CN_ITEM_RE = re.compile(r"^[一二三四五六七八九十百]+[、．.]")
_UNSAFE = re.compile(r'[:/\\*?"<>|\n\r\t]')


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def to_markdown(src: Path, tmp_dir: Path) -> str:
    """docx/doc → markdown 文本。失败抛 RuntimeError。"""
    suffix = src.suffix.lower()
    if suffix == ".docx":
        pandoc = _which("pandoc")
        if not pandoc:
            raise RuntimeError("未检测到 pandoc")
        out = tmp_dir / (src.stem + ".md")
        subprocess.run([pandoc, "-f", "docx", "-t", "markdown", "--wrap=none",
                        str(src), "-o", str(out)], check=True, capture_output=True)
        return out.read_text(encoding="utf-8", errors="replace")
    if suffix == ".doc":
        # macOS 自带 textutil；其他平台要求 libreoffice
        textutil = _which("textutil")
        if textutil:
            r = subprocess.run([textutil, "-convert", "txt", "-stdout", str(src)],
                               check=True, capture_output=True)
            return r.stdout.decode("utf-8", errors="replace")
        soffice = _which("soffice") or _which("libreoffice")
        if soffice:
            subprocess.run([soffice, "--headless", "--convert-to", "docx",
                            "--outdir", str(tmp_dir), str(src)],
                           check=True, capture_output=True, timeout=180)
            return to_markdown(tmp_dir / (src.stem + ".docx"), tmp_dir)
        raise RuntimeError("未检测到 textutil / libreoffice，无法转换 .doc")
    raise RuntimeError(f"不支持的格式：{src.suffix}")


def head_block(text: str, limit: int = 40) -> list[str]:
    """取标题到目录之前的部分，用于提公布日期。"""
    out: list[str] = []
    for line in text.splitlines()[:limit]:
        s = line.strip()
        # 「目　　录」「目录」之后是章节清单，里面的日期不是公布日期
        if re.fullmatch(r"[目目]\s*录|目录", s.replace("\u3000", "")):
            break
        out.append(s)
    return out


def extract_publish_date(text: str) -> str:
    """公布日期＝头部所有日期里最晚的那个（沿革按时间递增排列，最后一个才是本次）。

    必须排除未来日期：正文里会写「2047年6月30日前」这类期限，按字符串比较会被
    当成最新日期，把整部法的公布日期顶到 2047 年。
    """
    today = datetime.date.today().isoformat()
    dates: list[str] = []
    for s in head_block(text):
        for m in _DATE_RE.finditer(s):
            y, mo, d = m.groups()
            iso = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
            if "1900-01-01" < iso <= today:
                dates.append(iso)
    return max(dates) if dates else ""


def extract_effective_date(text: str) -> str:
    hits = _EFFECTIVE_RE.findall(text)
    if not hits:
        return ""
    y, mo, d = hits[-1]
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


def is_amendment(title: str) -> bool:
    """判断是否「关于…的决定」这类文件。

    原先只认「关于修改/关于废止」开头，漏掉了「关于集中修改、废止…的决定」这种
    中间夹了副词的说法——漏判的后果是通用解析器按「第X条」切分，切出 0 条，整份
    文件被静默跳过。
    """
    if "决定" not in title:
        return False
    if "决定" not in title:
        return False
    return "关于" in title


def body_lines(text: str) -> list[str]:
    """去掉首行标题、引用块（颁布信息）、空行后的正文行。"""
    lines: list[str] = []
    seen_title = False
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(">"):
            continue
        if not seen_title:
            # 第一非空行通常是法规标题本身
            seen_title = True
            continue
        lines.append(s)
    return lines


def build_markdown(title: str, text: str, amendment: bool) -> str:
    """生成给 parse_markdown 吃的 Markdown。

    普通法规：原样保留正文（章条结构由通用解析器切）。
    修改决定：压成「第1条　（全文）」+ 其余行作续行。
    """
    lines = body_lines(text)
    if not amendment:
        return "# " + title + "\n\n" + "\n\n".join(lines) + "\n"

    items = [ln for ln in lines if _CN_ITEM_RE.match(ln)]
    head = f"第1条\u3000（修改决定全文，共 {len(items)} 项修改内容，项号以「一、二、三」排列）"
    return "# " + title + "\n\n" + head + "\n\n" + "\n\n".join(lines) + "\n"


def law_type_for(path: str, title: str) -> tuple[str, str]:
    if is_amendment(title):
        return AMEND_TYPE
    prefix = path.split("/")[0][:2]
    return PATH_TYPE.get(prefix, LOCAL_TYPE)


def safe_name(name: str) -> str:
    return _UNSAFE.sub("", name).strip()


def main() -> None:
    ap = argparse.ArgumentParser(description="把镜像法规整理成 inbox Markdown")
    ap.add_argument("--index", default="/tmp/lawtext/data-docx.jsonl")
    ap.add_argument("--map", default="/tmp/lawtext/pull-map.json")
    ap.add_argument("--raw", default="/tmp/lawtext/raw")
    ap.add_argument("--inbox", default="inbox/mirror")
    ap.add_argument("--cutoff", default="20260701", help="只处理该日期之后更新的文件")
    ap.add_argument("--tmp", default="/tmp/lawtext/md")
    args = ap.parse_args()

    with open(args.map, encoding="utf-8") as f:
        items = json.load(f)
    cutoff = args.cutoff
    os.makedirs(args.inbox, exist_ok=True)
    os.makedirs(args.tmp, exist_ok=True)

    ok = skipped = 0
    no_date: list[str] = []
    no_effective: list[str] = []
    failed: list[tuple[str, str]] = []
    amendments: list[str] = []

    for it in items:
        title = it["title"]
        src = Path(it["file"])
        upd = (it.get("update_at") or "")
        digits = re.sub(r"\D", "", upd)[:8]
        if digits and digits < cutoff:
            skipped += 1
            continue
        if not src.exists():
            failed.append((title, "原件缺失"))
            continue
        try:
            text = to_markdown(src, Path(args.tmp))
        except Exception as exc:  # noqa: BLE001
            failed.append((title, f"转换失败：{exc}"))
            continue

        pub = extract_publish_date(text)
        eff = extract_effective_date(text)
        if not pub:
            no_date.append(title)
            continue
        if not eff:
            no_effective.append(title)
        amendment = is_amendment(title)
        if amendment:
            amendments.append(title)
        _lt, label = law_type_for(it.get("path", ""), title)

        md = build_markdown(title, text, amendment)
        fname = f"{safe_name(title)}_公布日期{pub}_施行日期{eff or pub}_法律法规分类{label}.md"
        (Path(args.inbox) / fname).write_text(md, encoding="utf-8")
        ok += 1

    print(f"写出 {ok} 份到 {args.inbox}（跳过 {skipped} 份：早于 {cutoff}）")
    if amendments:
        print(f"  其中修改/废止决定 {len(amendments)} 份")
    if no_effective:
        print(f"  [注意] {len(no_effective)} 份未提取到施行日期，已用公布日期兜底")
    if no_date:
        print(f"  [需人工] {len(no_date)} 份未提取到公布日期，未写入：")
        for t in no_date[:20]:
            print("    -", t)
    if failed:
        print(f"  [失败] {len(failed)} 份：")
        for t, why in failed[:20]:
            print(f"    - {t}｜{why}")
    with open("/tmp/lawtext/fetch-mirror-report.json", "w", encoding="utf-8") as f:
        json.dump({"ok": ok, "skipped": skipped, "no_date": no_date,
                   "no_effective": no_effective,
                   "failed": failed, "amendments": amendments},
                  f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
