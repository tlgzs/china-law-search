# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""发布前质检门禁（只读检测，不修数据、不改库）。

九条门禁来自《法规库勘察报告-20261105.md》第三节，逐条机器判定：

  G1 施行日↔效力双向一致      pending 必须未到施行日；effective 不能是未来施行日
  G2 同名唯一现行            同一 norm_name 只允许一个 status='effective'
  G3 生效区间不重叠          同法相邻版本施行日不得倒挂、不得同一天
  G4 页尾/域代码残渣          Word 域代码、页脚 CTA、跨语料重复尾行
  G5 缺收录                   应收录清单未入库；某批新增为 0 却跳过了文件
  G6 同文重复                 同名不同版本的条文哈希序列完全相同
  G7 条文切分异常             article_count=0、跳号率>5%、单条>5000 字、条号重复
                           （跳号仅 parse_gap 阻断；source_gap 降为告警，见豁免清单）
  G8 变更未换版本号           库哈希/manifest 不符、VERSION 与 manifest 不符、corpus_version 未 bump
  G9 元数据自洽               manifest 的部数/条数与库重算不符、产物 sha256 不符

用法：
  python tools/release_gate.py --db data/law_data.db --manifest dist/manifest.json
  python tools/release_gate.py --db data/law_data.db --only G1,G2,G4,G7
  python tools/release_gate.py --db <解压后的发布库> --manifest dist/manifest.json --artifacts-dir dist
  python tools/release_gate.py ... --json reports/gate-report.json

退出码：0 = 无阻断项；1 = 有阻断项；2 = 参数/读取失败。
  --strict 时告警也算失败（CI 里想全绿时用）。

设计约束：
  - 只用标准库，不联网，只读打开数据库（PRAGMA query_only=1），不改任何资产
  - 每条门禁独立可跑（--only/--skip），失败文案固定，便于 CI 解析
"""
from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
SKIP = "SKIP"

BLOCKING = "block"
ADVISORY = "warn"

# 门禁编号 → (标题, 报告规定的最严动作, 拦截位置, 失败文案)
GATES = {
    "G1": ("施行日↔效力双向一致", BLOCKING, "打包前 preflight",
           "存在效力状态与施行日矛盾的版本：pending 却已到/已过施行日，或 effective 却是未来施行日"),
    "G2": ("同名唯一现行", BLOCKING, "打包前 preflight",
           "同一法规存在多个现行有效版本，检索会取到不确定的一版"),
    "G3": ("生效区间不重叠", BLOCKING, "打包前 preflight",
           "同一法规的版本施行区间倒挂、重叠或同日生效，按时间点选版会选错"),
    "G4": ("页尾/域代码残渣", BLOCKING, "打包前 preflight",
           "正文残留 Word 域代码或页脚 CTA，用户会看到「PAGE * MERGEFORMAT」这类垃圾"),
    "G5": ("缺收录", BLOCKING, "打包前 preflight",
           "应收录清单中的法规未入库（或某批只跳过文件、没新增任何法规）"),
    "G6": ("同文重复", ADVISORY, "打包前 preflight（告警）",
           "同一法规不同版本的条文序列完全相同，可能是重复转换件而非真修订"),
    "G7": ("条文切分异常", BLOCKING, "打包前 preflight",
           "条文切分异常：0 条、跳号率超 5%、单条超 5000 字或条号重复"),
    "G8": ("变更未换版本号", BLOCKING, "打包后 / 发布前校验",
           "库文件与 manifest 不一致：改了库没重新打包，或 corpus_version 未 bump，或 VERSION 与 manifest.app_version 不一致"),
    "G9": ("元数据自洽", BLOCKING, "打包后 / 发布前校验",
           "manifest 的部数/条数与库重算不符，或产物 sha256 与实际文件不符"),
}

_MAX_SAMPLES = 8


@dataclass
class GateResult:
    gate: str
    status: str = PASS
    stats: dict = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)
    samples: list[str] = field(default_factory=list)
    message: str = ""

    @property
    def title(self) -> str:
        return GATES[self.gate][0]

    @property
    def severity(self) -> str:
        return GATES[self.gate][1]

    def fail(self, message: str) -> "GateResult":
        self.status = FAIL
        self.message = message
        return self

    def warn(self, message: str) -> "GateResult":
        if self.status != FAIL:
            self.status = WARN
        self.message = message
        return self

    def sample(self, text: str) -> None:
        if len(self.samples) < _MAX_SAMPLES:
            self.samples.append(text)


def _today_default() -> str:
    import datetime
    return datetime.date.today().isoformat()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _connect(db_path: str) -> sqlite3.Connection:
    if not os.path.exists(db_path):
        raise SystemExit(f"库文件不存在：{db_path}")
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    return conn


def _require_tables(conn: sqlite3.Connection, names: list[str]) -> None:
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    missing = [n for n in names if n not in existing]
    if missing:
        raise SystemExit(f"库缺少表：{', '.join(missing)}（不是本项目的法规库？）")


# ============================================================
# G1 施行日↔效力双向一致
# ============================================================

def check_g1(conn: sqlite3.Connection, today: str) -> GateResult:
    r = GateResult("G1")
    pending_bad = conn.execute(
        "SELECT id, law_name, publish_date, effective_date FROM law_versions "
        "WHERE status='pending' AND (effective_date IS NULL OR effective_date='' "
        "OR effective_date <= ?)", (today,)).fetchall()
    effective_bad = conn.execute(
        "SELECT id, law_name, publish_date, effective_date FROM law_versions "
        "WHERE status='effective' AND effective_date > ?", (today,)).fetchall()
    r.stats = {"pending_violations": len(pending_bad), "effective_violations": len(effective_bad),
               "today": today}
    for row in pending_bad:
        r.sample(f"待生效但已到施行日：{row['law_name']}（施行 {row['effective_date'] or '缺失'}）")
    for row in effective_bad:
        r.sample(f"现行但施行日在未来：{row['law_name']}（施行 {row['effective_date']}）")
    if pending_bad or effective_bad:
        r.fail(GATES["G1"][3])
    return r


# ============================================================
# G2 同名唯一现行
# ============================================================

def check_g2(conn: sqlite3.Connection, today: str) -> GateResult:
    r = GateResult("G2")
    rows = conn.execute(
        "SELECT norm_name, COUNT(*) AS n FROM law_versions "
        "WHERE status='effective' GROUP BY norm_name HAVING n > 1 "
        "ORDER BY n DESC").fetchall()
    r.stats = {"conflicts": len(rows)}
    for row in rows:
        r.sample(f"{row['norm_name']}：{row['n']} 个现行版本")
    if rows:
        r.fail(GATES["G2"][3])
    return r


# ============================================================
# G3 生效区间不重叠
# ============================================================

def check_g3(conn: sqlite3.Connection, today: str) -> GateResult:
    r = GateResult("G3")
    rows = conn.execute(
        "SELECT id, norm_name, law_name, publish_date, effective_date, status, "
        "superseded_at FROM law_versions "
        "ORDER BY norm_name, COALESCE(publish_date, ''), id").fetchall()

    by_law: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        by_law[row["norm_name"]].append(row)

    backwards, same_day = [], []
    timestamp_like = 0
    for norm, versions in by_law.items():
        dated = [v for v in versions if v["effective_date"]]
        if not dated:
            continue
        # 1) 按公布日排序后，施行日不得倒挂：新版只能比旧版晚生效
        prev = None
        for v in dated:
            if prev and v["effective_date"] < prev["effective_date"]:
                backwards.append((norm, prev, v))
            prev = v
        # 2) 同一天生效的两版 = 零长度区间，按时间点选版必然二选一
        seen: dict[str, sqlite3.Row] = {}
        for v in dated:
            other = seen.get(v["effective_date"])
            if other is not None:
                same_day.append((norm, other, v))
            else:
                seen[v["effective_date"]] = v
        # 3) superseded_at 只做信息项：实测 252 个非空值里 192 个是
        #    「2026-11-03T02:12:00Z」这类**执行时间戳**，不是法律上的失效日，
        #    拿它和下一版施行日比对必然误判。区间一律用 effective_date 推。
        for v in versions:
            value = v["superseded_at"] or ""
            if "T" in value or " " in value:
                timestamp_like += 1

    r.stats = {"backwards": len(backwards), "same_day": len(same_day),
               "superseded_at_is_timestamp": timestamp_like,
               "laws_with_versions": len(by_law)}
    for norm, a, b in backwards:
        r.sample(f"施行日倒挂：{norm}（{a['publish_date']} 版施行 {a['effective_date']} → "
                 f"{b['publish_date']} 版施行 {b['effective_date']}）")
    for norm, a, b in same_day:
        r.sample(f"同日生效：{norm}（{a['publish_date']} / {b['publish_date']} 均施行 {a['effective_date']}）")
    if timestamp_like:
        r.sample(f"另有 {timestamp_like} 个 superseded_at 是执行时间戳，未参与区间判定")

    if backwards or same_day:
        r.fail(GATES["G3"][3])
    return r


# ============================================================
# G4 页尾/域代码残渣
# ============================================================

_FIELD_CODE_PATTERNS = [
    ("PAGE*MERGEFORMAT", re.compile(r"PAGE\s*\\?\*\s*MERGEFORMAT")),
    ("\\*MERGEFORMAT", re.compile(r"\\\*\s*MERGEFORMAT")),
    ("HYPERLINK", re.compile(r"HYPERLINK")),
    ("SHAPE", re.compile(r"SHAPE\s+\\\*")),
    ("EMBED", re.compile(r"\bEMBED\b")),
    ("NUMPAGES", re.compile(r"\bNUMPAGES\b")),
    ("SECTIONPAGES", re.compile(r"\bSECTIONPAGES\b")),
    ("PAGEREF", re.compile(r"\bPAGEREF\b")),
    ("MERGEFIELD", re.compile(r"\bMERGEFIELD\b")),
    ("_Toc", re.compile(r"\\l\s+\"_Toc")),
    ("TOC", re.compile(r"\bTOC\s+\\o")),
]
_FIELD_CODE_HINT = re.compile(r"MERGEFORMAT|HYPERLINK|SHAPE|EMBED|PAGE|MERGEFIELD|TOC")
_TAIL_CTA_BLOCK = ("扫一扫", "手机阅读", "责任编辑", "扫描二维码", "关注公众号")
_TAIL_CTA_WARN = ("二维码",)
_TAIL_LIMIT = 200
_REPEAT_TAIL_MIN_LAWS = 10
# 跨语料重复尾行不能只看「重复 ≥10 部」：实测按该口径命中 1,184 行，其中
# 1,180 行是「本条例自公布之日起施行。」这类合法条文。只有带站点/版权/扫码
# 特征的重复尾行才是页脚残留，所以先过这一层特征，再判重复度。
_RESIDUE_TAIL_RE = re.compile(
    r"https?://|www\.|版权所有|Copyright|©|扫码|扫一扫|手机阅读|下载|关注|公众|"
    r"公众号|责任编辑|来源[:：]|PAGE|MERGEFORMAT|二维码|法律咨询|热线")


def check_g4(conn: sqlite3.Connection, today: str) -> GateResult:
    r = GateResult("G4")
    pattern_counts = Counter()
    pattern_laws: dict[str, set] = defaultdict(set)
    pattern_samples: dict[str, list[str]] = defaultdict(list)
    tail_cta = Counter()
    tail_cta_laws: dict[str, set] = defaultdict(set)
    tail_line_laws: dict[str, set] = defaultdict(set)

    matched_rows = 0
    for law_name, content in conn.execute("SELECT law_name, content FROM law_articles"):
        text = content or ""
        if _FIELD_CODE_HINT.search(text):
            hit = False
            for label, pattern in _FIELD_CODE_PATTERNS:
                m = pattern.search(text)
                if not m:
                    continue
                hit = True
                pattern_counts[label] += 1
                pattern_laws[label].add(law_name)
                if len(pattern_samples[label]) < 3:
                    start = max(0, m.start() - 30)
                    pattern_samples[label].append(
                        f"{law_name}：…{text[start:m.end() + 30]}…")
            if hit:
                matched_rows += 1
        tail = text[-_TAIL_LIMIT:]
        for word in _TAIL_CTA_BLOCK + _TAIL_CTA_WARN:
            if word in tail:
                tail_cta[word] += 1
                tail_cta_laws[word].add(law_name)
        last_line = text.rstrip().rsplit("\n", 1)[-1].strip()
        if 2 <= len(last_line) <= 40 and _RESIDUE_TAIL_RE.search(last_line):
            tail_line_laws[last_line].add(law_name)

    blocking_patterns = {k: v for k, v in pattern_counts.items() if v}
    r.stats = {
        "field_code_rows": matched_rows,
        "field_code_by_pattern": {k: {"rows": v, "laws": len(pattern_laws[k])}
                                  for k, v in blocking_patterns.items()},
        "tail_cta_block": {k: v for k, v in tail_cta.items() if k in _TAIL_CTA_BLOCK},
        "tail_cta_warn": {k: v for k, v in tail_cta.items() if k in _TAIL_CTA_WARN},
    }
    for label, count in blocking_patterns.items():
        r.sample(f"{label}：{count} 条 / {len(pattern_laws[label])} 部")
    for label, samples in pattern_samples.items():
        for s in samples[:1]:
            r.sample(f"{label} 样本 {s}")

    repeated = [(line, laws) for line, laws in tail_line_laws.items()
                if len(laws) >= _REPEAT_TAIL_MIN_LAWS]
    repeated.sort(key=lambda kv: -len(kv[1]))
    r.stats["repeated_tail_lines"] = len(repeated)
    for line, laws in repeated[:3]:
        r.sample(f"重复尾行（{len(laws)} 部）：{line}")

    blocked = bool(blocking_patterns) or any(v for k, v in tail_cta.items() if k in _TAIL_CTA_BLOCK)
    if blocked:
        r.fail(GATES["G4"][3])
    elif repeated or any(v for k, v in tail_cta.items() if k in _TAIL_CTA_WARN):
        r.warn("存在跨语料重复尾行或疑似页脚文字（告警）")
    return r


# ============================================================
# G5 缺收录
# ============================================================

def check_g5(conn: sqlite3.Connection, today: str, catalog: str | None,
             plans_dir: str | None) -> GateResult:
    r = GateResult("G5")
    missing, stale = [], []
    if catalog and os.path.exists(catalog):
        with open(catalog, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                norm = (row.get("norm_name") or "").strip()
                latest = (row.get("latest_publish_date") or "").strip()
                if not norm:
                    continue
                # 不能拿 MAX(publish_date) 是否为空判断「有没有入库」：
                # 库里存在公布日为空（''）的版本，MAX 会返回 ''，会被误判成未入库。
                # 先数存在性，再在双方都有日期时比新旧。
                count, hit = conn.execute(
                    "SELECT COUNT(*), MAX(NULLIF(publish_date, '')) "
                    "FROM law_versions WHERE norm_name = ?",
                    (norm,)).fetchone()
                if not count:
                    missing.append(norm)
                elif latest and hit and hit < latest:
                    stale.append((norm, latest, hit))
        r.stats = {"catalog": catalog, "missing": len(missing), "stale": len(stale)}
    else:
        r.status = SKIP
        r.message = "未提供应收录清单（--catalog），G5 的清单比对部分跳过"
        r.stats["catalog"] = "未提供"

    batch_failures = []
    if plans_dir and os.path.isdir(plans_dir):
        for path in sorted(glob.glob(os.path.join(plans_dir, "**", "*.json"), recursive=True)):
            if ".plan." not in os.path.basename(path) and not os.path.basename(path).endswith("plan.json"):
                continue
            try:
                with open(path, encoding="utf-8-sig") as f:
                    plan = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            summary = plan.get("summary") or {}
            added = summary.get("new_laws", summary.get("added_laws", 0)) or 0
            skipped = summary.get("skipped_laws", summary.get("skipped", 0)) or 0
            if added == 0 and skipped > 0:
                batch_failures.append((os.path.basename(path), skipped))
    r.stats["batches_without_new_laws"] = len(batch_failures)
    for name, skipped in batch_failures:
        r.sample(f"{name}：新增 0 部、跳过 {skipped} 个文件")
    for norm in missing[:3]:
        r.sample(f"清单内未入库：{norm}")

    if missing or batch_failures:
        r.fail(GATES["G5"][3])
    elif stale:
        r.warn(f"清单内 {len(stale)} 部法规库内公布日落后于清单")
    if r.status == SKIP and not batch_failures:
        r.message = "未提供应收录清单（--catalog），G5 的清单比对部分跳过"
    return r


# ============================================================
# G6 同文重复
# ============================================================

def check_g6(conn: sqlite3.Connection, today: str) -> GateResult:
    r = GateResult("G6")
    rows = conn.execute(
        "SELECT version_id, article_number_num, COALESCE(article_suffix,'') AS suffix, "
        "content_hash FROM law_articles ORDER BY version_id, article_number_num, suffix")
    seq: dict[int, list[str]] = defaultdict(list)
    for version_id, _num, _suffix, content_hash in rows:
        seq[version_id].append(content_hash or "")

    versions = conn.execute(
        "SELECT id, norm_name, law_name, publish_date, effective_date, article_count "
        "FROM law_versions ORDER BY norm_name, COALESCE(publish_date, ''), id").fetchall()
    by_law: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for v in versions:
        by_law[v["norm_name"]].append(v)

    duplicates = []
    for norm, items in by_law.items():
        if len(items) < 2:
            continue
        seen: dict[tuple, sqlite3.Row] = {}
        for v in items:
            hashes = tuple(seq.get(v["id"], ()))
            if not hashes:
                continue
            other = seen.get(hashes)
            if other is not None:
                duplicates.append((norm, other, v))
            else:
                seen[hashes] = v
    r.stats = {"laws_multi_version": sum(1 for items in by_law.values() if len(items) > 1),
               "duplicate_pairs": len(duplicates)}
    for norm, a, b in duplicates[:3]:
        r.sample(f"{norm}：{a['publish_date'] or '无日期'} 与 {b['publish_date'] or '无日期'} 条文哈希完全一致")
    if duplicates:
        r.warn(f"{len(duplicates)} 组同名版本条文完全相同（可能是重复转换件）")
    return r


# ============================================================
# G7 条文切分异常
# ============================================================

_LONG_ARTICLE = 5000
_JUMP_RATE_LIMIT = 0.05
# 孤立高号与正常条号序列之间至少要空出这么多号，才认定为引用而非真缺。
_ISOLATED_JUMP = 5


def _trim_isolated_numbers(nums: list[int]) -> list[int]:
    """剔除被正文续写或他法引用带进来的孤立高号。

    例如民间借贷规定正文续行「第五十二条以及本规定第十四条之规定…」曾被切成
    一个独立条号，把最大条号抬到 52。只从序列尾部逐个大号往回收，且仅当该号与
    前一个条号的间距超过 _ISOLATED_JUMP 才剔除；间距正常的条号一律保留，
    避免把「真的缺一小段」误判成引用。剔除后为空则保持原样（决定、解释类文书
    本身没有条号序列，交给体裁判定）。
    """
    kept = sorted(set(nums))
    while len(kept) > 1 and kept[-1] - kept[-2] > _ISOLATED_JUMP:
        kept.pop()
    return kept


# G7 缺口豁免清单：已定性为「源文件本身缺条号」的版本清单。
# 只降级 source_gap、排除 amendment；未登记或登记为 parse_gap 的版本照常阻断。
_G7_REGISTRY_DEFAULT = os.path.join(_ROOT, "reports", "g7-gap-registry-20261107.csv")


def _load_gap_registry(path: str | None) -> dict[int, dict]:
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return {int(r["version_id"]): r for r in rows if (r.get("version_id") or "").strip()}


def check_g7(conn: sqlite3.Connection, today: str, registry_path: str | None = None) -> GateResult:
    r = GateResult("G7")
    registry = _load_gap_registry(registry_path)
    empty_versions = conn.execute(
        "SELECT id, law_name, publish_date, article_count FROM law_versions "
        "WHERE article_count = 0").fetchall()
    duplicate_keys = conn.execute(
        "SELECT version_id, article_number_num, COALESCE(article_suffix,'') AS suffix, "
        "COUNT(*) AS n FROM law_articles GROUP BY 1,2,3 HAVING n > 1 LIMIT 50").fetchall()
    long_rows = conn.execute(
        "SELECT a.version_id, v.law_name, a.article_number, LENGTH(a.content) AS n, "
        "v.law_type FROM law_articles a JOIN law_versions v ON v.id = a.version_id "
        "WHERE LENGTH(a.content) > ? AND v.law_type != 'amendment' "
        "ORDER BY n DESC LIMIT 200", (_LONG_ARTICLE,)).fetchall()
    long_articles = []
    source_long_articles = []
    for row in long_rows:
        cause = (registry.get(row["version_id"], {}).get("root_cause") or "").strip()
        if cause == "amendment":
            # 修改决定整体退出超长检查（如 20347）。
            continue
        if cause == "source_long":
            source_long_articles.append(row)
        else:
            long_articles.append(row)

    jumps = []
    parse_gap_jumps = []
    source_gap_jumps = []
    rows = conn.execute(
        "SELECT v.id, v.law_name, v.law_type, COUNT(DISTINCT a.article_number_num) AS n, "
        "MAX(a.article_number_num) AS mx FROM law_versions v "
        "JOIN law_articles a ON a.version_id = v.id GROUP BY v.id").fetchall()
    for row in rows:
        if row["law_type"] in ("amendment", "other"):
            continue
        info = registry.get(row["id"])
        # 修改决定不按条号连续性判：豁免清单登记为 amendment 的版本直接退出统计。
        if info and (info.get("root_cause") or "").strip() == "amendment":
            continue
        maximum = row["mx"] or 0
        if maximum <= 0:
            continue
        rate = 1 - (row["n"] or 0) / maximum
        if rate > _JUMP_RATE_LIMIT:
            # 只用本文实际条号序列：剔除正文续写/引用带来的孤立高号后重算。
            nums = [
                x[0] for x in conn.execute(
                    "SELECT article_number_num FROM law_articles "
                    "WHERE version_id = ? AND article_number_num > 0", (row["id"],))
            ]
            trimmed = _trim_isolated_numbers(nums)
            if trimmed:
                maximum = max(trimmed)
                rate = 1 - len(set(trimmed)) / maximum
        if rate > _JUMP_RATE_LIMIT:
            jumps.append((rate, row["law_name"], row["n"], maximum))
            if info and (info.get("root_cause") or "").strip() == "source_gap":
                source_gap_jumps.append((rate, row, info))
            else:
                parse_gap_jumps.append((rate, row, info))
    jumps.sort(reverse=True)
    parse_gap_jumps.sort(key=lambda x: x[0], reverse=True)
    source_gap_jumps.sort(key=lambda x: x[0], reverse=True)

    source_gap_missing = sum(int(float((x[2].get("missing_numbers_total") or 0))) for x in source_gap_jumps)
    registry_rel = os.path.relpath(registry_path or _G7_REGISTRY_DEFAULT, _ROOT)
    r.stats = {
        "empty_versions": len(empty_versions),
        "duplicate_article_keys": len(duplicate_keys),
        "long_articles": len(long_articles),
        "source_long_advisory": len(source_long_articles),
        "source_long_versions": len({row["version_id"] for row in source_long_articles}),
        "long_article_threshold": _LONG_ARTICLE,
        "jump_rate_violations": len(jumps),
        "jump_rate_limit": _JUMP_RATE_LIMIT,
        "parse_gap_blocking": len(parse_gap_jumps),
        "source_gap_advisory": len(source_gap_jumps),
        "source_gap_missing_numbers": source_gap_missing,
        "gap_registry_entries": len(registry),
        "gap_registry": registry_rel,
    }
    for row in empty_versions[:3]:
        r.sample(f"0 条版本：{row['law_name']}（{row['publish_date'] or '无日期'}）")
    for row in duplicate_keys[:2]:
        r.sample(f"条号重复：version {row['version_id']} 第 {row['article_number_num']} 条 × {row['n']}")
    for row in long_articles[:3]:
        r.sample(f"超长条文：{row['law_name']} {row['article_number']} = {row['n']} 字")
    for row in source_long_articles[:3]:
        r.sample(f"源侧超长（source_long）：{row['law_name']} {row['article_number']} = {row['n']} 字，保留整条不拆")
    for rate, row, info in parse_gap_jumps[:3]:
        r.sample(f"跳号率 {rate:.1%}（parse_gap）：{row['law_name']}（{row['n']}/{row['mx']} 条）")
    for rate, row, info in source_gap_jumps[:3]:
        r.sample(
            f"源缺号（source_gap）：version {row['id']} {row['law_name']} "
            f"缺 {info.get('missing_ranges') or info.get('missing_numbers_total')}；"
            f"源文件 {os.path.basename(info.get('source_file') or '')}，"
            f"文件内条号 {info.get('src_min')}–{info.get('src_max')}")

    structural = bool(empty_versions or duplicate_keys or long_articles)
    if structural or parse_gap_jumps:
        r.fail(GATES["G7"][3])
    elif source_gap_jumps or source_long_articles:
        parts = []
        if source_gap_jumps:
            parts.append(f"{len(source_gap_jumps)} 个版本源文件缺条号（source_gap）")
        if source_long_articles:
            parts.append(f"{len(source_long_articles)} 条源文件本身超长（source_long）")
        r.warn("，".join(parts) + f" 不阻断；明细见 {registry_rel}")
    return r


# ============================================================
# G8 变更未换版本号
# ============================================================

_VERSION_RE = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})\.(\d+)$")


def _read_text(path: str | None) -> str:
    if not path or not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def check_g8(conn: sqlite3.Connection, today: str, db_path: str, manifest_path: str | None,
             version_file: str | None, previous_manifest: str | None) -> GateResult:
    r = GateResult("G8")
    db_sha = sha256_file(db_path)
    db_version = ""
    meta_rows = conn.execute("SELECT key, value FROM corpus_meta").fetchall()
    for key, value in meta_rows:
        if key == "corpus_version":
            db_version = value or ""
    version_text = _read_text(version_file)

    problems = []
    stats = {"db_sha256": db_sha, "corpus_version": db_version, "version_file": version_text}

    if not _VERSION_RE.match(db_version):
        problems.append(f"corpus_version 格式不合规：{db_version or '缺失'}（应为 YYYY.MM.DD.N）")

    manifest = None
    if manifest_path and os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8-sig") as f:
            manifest = json.load(f)
    else:
        problems.append(f"manifest 缺失：{manifest_path or '未指定'}")
        stats["manifest"] = "缺失"

    if manifest:
        stats["manifest_db_sha256"] = manifest.get("db_sha256")
        stats["manifest_app_version"] = manifest.get("app_version")
        stats["manifest_corpus_version"] = manifest.get("corpus_version")
        if manifest.get("db_sha256") != db_sha:
            problems.append(
                f"库 sha256 与 manifest 不符：库 {db_sha[:16]}… / manifest "
                f"{(manifest.get('db_sha256') or '缺失')[:16]}… —— 改了库没重新打包")
        if version_text and manifest.get("app_version") != version_text:
            problems.append(
                f"VERSION({version_text}) 与 manifest.app_version"
                f"({manifest.get('app_version')}) 不一致 —— 打包后升了程序版本却没重打包")
        if db_version and manifest.get("corpus_version") != db_version:
            problems.append(
                f"库 corpus_version({db_version}) 与 manifest.corpus_version"
                f"({manifest.get('corpus_version')}) 不一致")

    if previous_manifest and os.path.exists(previous_manifest):
        with open(previous_manifest, encoding="utf-8-sig") as f:
            prev = json.load(f)
        prev_version = prev.get("corpus_version") or ""
        prev_sha = prev.get("db_sha256") or ""
        stats["previous_corpus_version"] = prev_version
        if prev_sha and prev_sha != db_sha:
            if not _bump_ok(prev_version, db_version):
                problems.append(
                    f"库已变更但 corpus_version 未 bump：{prev_version or '缺失'} → {db_version or '缺失'}")

    r.stats = stats
    for p in problems:
        r.sample(p)
    if problems:
        r.fail(GATES["G8"][3])
    return r


def _bump_ok(prev: str, current: str) -> bool:
    a, b = _VERSION_RE.match(prev or ""), _VERSION_RE.match(current or "")
    if not (a and b):
        return bool(current) and current != prev
    return tuple(int(x) for x in b.groups()) > tuple(int(x) for x in a.groups())


# ============================================================
# G9 元数据自洽
# ============================================================

def check_g9(conn: sqlite3.Connection, today: str, manifest_path: str | None,
             artifacts_dir: str | None) -> GateResult:
    r = GateResult("G9")
    if not manifest_path or not os.path.exists(manifest_path):
        r.status = SKIP
        r.message = "未提供 manifest，G9 跳过"
        return r
    with open(manifest_path, encoding="utf-8-sig") as f:
        manifest = json.load(f)

    # 与 tools/make_release.py 完全相同的口径：
    #   article_count = status != 'repealed' 的条文数
    #   law_count     = status='effective' 的不同 norm_name 数
    articles = conn.execute(
        "SELECT COUNT(*) FROM law_articles WHERE status != 'repealed'").fetchone()[0]
    laws = conn.execute(
        "SELECT COUNT(DISTINCT norm_name) FROM law_versions WHERE status='effective'").fetchone()[0]
    problems = []
    stats = {"db_article_count": articles, "manifest_article_count": manifest.get("article_count"),
             "db_law_count": laws, "manifest_law_count": manifest.get("law_count")}
    if manifest.get("article_count") != articles:
        problems.append(f"article_count 不符：manifest {manifest.get('article_count')} / 实算 {articles}")
    if manifest.get("law_count") != laws:
        problems.append(f"law_count 不符：manifest {manifest.get('law_count')} / 实算 {laws}")

    artifact_stats = []
    for item in manifest.get("artifacts") or []:
        name = item.get("name") or ""
        path = os.path.join(artifacts_dir or "", name) if artifacts_dir else ""
        if not path or not os.path.exists(path):
            problems.append(f"产物缺失：{name}")
            artifact_stats.append({"name": name, "status": "missing"})
            continue
        actual_sha = sha256_file(path)
        actual_size = os.path.getsize(path)
        ok_sha = actual_sha == item.get("sha256")
        ok_size = actual_size == item.get("size")
        artifact_stats.append({"name": name, "sha256_ok": ok_sha, "size_ok": ok_size})
        if not ok_sha:
            problems.append(f"产物 sha256 不符：{name}（实际 {actual_sha[:16]}…）")
        if not ok_size:
            problems.append(f"产物体积不符：{name}（实际 {actual_size} 字节，manifest {item.get('size')}）")
    stats["artifacts"] = artifact_stats
    r.stats = stats
    for p in problems:
        r.sample(p)
    if problems:
        r.fail(GATES["G9"][3])
    return r


# ============================================================
# 主流程
# ============================================================

def run(db_path: str, manifest_path: str | None, version_file: str | None,
        artifacts_dir: str | None, plans_dir: str | None, catalog: str | None,
        previous_manifest: str | None, today: str, only: list[str] | None,
        g7_registry: str | None = None) -> list[GateResult]:
    conn = _connect(db_path)
    _require_tables(conn, ["law_articles", "law_versions", "corpus_meta"])
    selected = [g for g in GATES if not only or g in only]
    results: list[GateResult] = []
    for gate in selected:
        if gate == "G1":
            results.append(check_g1(conn, today))
        elif gate == "G2":
            results.append(check_g2(conn, today))
        elif gate == "G3":
            results.append(check_g3(conn, today))
        elif gate == "G4":
            results.append(check_g4(conn, today))
        elif gate == "G5":
            results.append(check_g5(conn, today, catalog, plans_dir))
        elif gate == "G6":
            results.append(check_g6(conn, today))
        elif gate == "G7":
            results.append(check_g7(conn, today, g7_registry))
        elif gate == "G8":
            results.append(check_g8(conn, today, db_path, manifest_path, version_file, previous_manifest))
        elif gate == "G9":
            results.append(check_g9(conn, today, manifest_path, artifacts_dir))
    conn.close()
    return results


def print_report(results: list[GateResult], db_path: str) -> None:
    print(f"法规库发布门禁 · 库 {db_path} · {_today_default()}")
    print("=" * 72)
    for r in results:
        tag = {"PASS": "通过", "FAIL": "阻断", "WARN": "告警", "SKIP": "跳过"}[r.status]
        print(f"[{r.gate}] {r.title:<18} {tag}  {r.message if r.status in (FAIL, SKIP) else ''}")
        if r.status in (FAIL, WARN, SKIP) or r.stats:
            for key, value in r.stats.items():
                if isinstance(value, dict):
                    if value:
                        print(f"      {key}: {json.dumps(value, ensure_ascii=False)}")
                elif value not in (0, "", None):
                    print(f"      {key}: {value}")
        for sample in r.samples:
            print(f"      · {sample}")
    print("=" * 72)
    fails = [r.gate for r in results if r.status == FAIL]
    warns = [r.gate for r in results if r.status == WARN]
    skips = [r.gate for r in results if r.status == SKIP]
    print(f"结果：阻断 {len(fails)}{'（' + ','.join(fails) + '）' if fails else ''}｜"
          f"告警 {len(warns)}{'（' + ','.join(warns) + '）' if warns else ''}｜"
          f"跳过 {len(skips)}{'（' + ','.join(skips) + '）' if skips else ''}")


def dump_json(results: list[GateResult], path: str, db_path: str) -> None:
    payload = {
        "generated_at": _today_default(),
        "db": os.path.abspath(db_path),
        "gates": [{
            "gate": r.gate, "title": r.title, "severity": r.severity,
            "status": r.status, "message": r.message, "stats": r.stats,
            "samples": r.samples,
        } for r in results],
    }
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="法规库发布前质检门禁（只读）")
    parser.add_argument("--db", default=os.path.join(_ROOT, "data", "law_data.db"))
    parser.add_argument("--manifest", default=os.path.join(_ROOT, "dist", "manifest.json"))
    parser.add_argument("--version-file", default=os.path.join(_ROOT, "VERSION"))
    parser.add_argument("--artifacts-dir", default=os.path.join(_ROOT, "dist"))
    parser.add_argument("--plans-dir", default=os.path.join(_ROOT, "reports"))
    parser.add_argument("--catalog", default=None, help="应收录清单 CSV（norm_name,latest_publish_date）")
    parser.add_argument("--g7-registry", default=_G7_REGISTRY_DEFAULT,
                        help="G7 缺口豁免清单 CSV（source_gap 降告警、amendment 退出统计）")
    parser.add_argument("--previous-manifest", default=None, help="上一版 manifest，用于判断是否 bump")
    parser.add_argument("--today", default=_today_default())
    parser.add_argument("--only", default=None, help="只跑指定门禁，逗号分隔，如 G1,G2,G4")
    parser.add_argument("--skip", default=None, help="跳过指定门禁，逗号分隔")
    parser.add_argument("--strict", action="store_true", help="告警也算失败")
    parser.add_argument("--json", default=None, help="输出 JSON 报告路径")
    args = parser.parse_args(argv)

    only = [g.strip().upper() for g in args.only.split(",")] if args.only else None
    if only:
        unknown = [g for g in only if g not in GATES]
        if unknown:
            print(f"未知门禁：{','.join(unknown)}", file=sys.stderr)
            return 2
    if args.skip:
        skipped = {g.strip().upper() for g in args.skip.split(",")}
        only = [g for g in (only or GATES) if g not in skipped]

    try:
        results = run(args.db, args.manifest, args.version_file, args.artifacts_dir,
                      args.plans_dir, args.catalog, args.previous_manifest, args.today, only,
                      args.g7_registry)
    except sqlite3.OperationalError as exc:
        print(f"读库失败：{exc}", file=sys.stderr)
        return 2

    print_report(results, args.db)
    if args.json:
        dump_json(results, args.json, args.db)

    failed = any(r.status == FAIL for r in results)
    warned = any(r.status == WARN for r in results) and args.strict
    return 1 if (failed or warned) else 0


if __name__ == "__main__":
    raise SystemExit(main())
