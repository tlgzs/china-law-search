# 壹律从简  www.yilvai.com
# 作者：网络小菜鸟
# 微信：pmfumin

"""发布门禁自检：构造干净库与坏样本库，验证九条门禁能放行/拦截。

用法：
  python tools/release_gate_selftest.py

退出码：0 = 所有断言符合预期；1 = 有门禁没按预期动作。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, os.path.join(_ROOT, "ingest"))

from schema import content_hash, ensure_schema, meta_set  # noqa: E402

TODAY = "2026-11-05"
APP_VERSION = "2.3.5"


def _version(conn, law_name, norm_name, publish, effective, status,
             articles, article_count=None, law_type="law"):
    cur = conn.execute(
        "INSERT INTO law_versions(law_name, norm_name, law_type, publish_date, effective_date, "
        "source_file, article_count, status) VALUES (?,?,?,?,?,?,?,?)",
        (law_name, norm_name, law_type, publish, effective, f"{norm_name}.md",
         len(articles) if article_count is None else article_count, status))
    version_id = int(cur.lastrowid)
    for num, text in articles:
        conn.execute(
            "INSERT INTO law_articles(vector_id, version_id, law_name, law_type, "
            "article_number, article_number_num, article_suffix, article, content, "
            "content_hash, publish_date, effective_date, status, source_file) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"{version_id}|{num}", version_id, law_name, law_type,
             f"第{num}条", num, "", text, text, content_hash(text),
             publish, effective, "repealed" if status == "superseded" else "effective",
             f"{norm_name}.md"))
    return version_id


def _sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build_good(db_path, meta):
    conn = sqlite3.connect(db_path)
    ensure_schema(conn)
    _version(conn, "中华人民共和国劳动合同法", "劳动合同法", "2012-12-28", "2013-07-01",
             "superseded", [(1, "第一条 为了完善劳动合同制度。"), (2, "第二条 适用范围。")])
    _version(conn, "中华人民共和国劳动合同法", "劳动合同法", "2025-01-01", "2026-01-01",
             "effective", [(1, "第一条 为了完善劳动合同制度，保护劳动者权益。"),
                           (2, "第二条 适用范围及除外。")])
    _version(conn, "中华人民共和国社会保险法", "社会保险法", "2026-09-01", "2027-01-01",
             "pending", [(1, "第一条 为了规范社会保险关系。")])
    meta_set(conn, "corpus_version", meta)
    conn.commit()
    conn.close()


def build_bad(db_path, meta):
    build_good(db_path, meta)
    conn = sqlite3.connect(db_path)
    # G1：待生效却已过施行日
    _version(conn, "待生效测试法", "待生效测试法", "2019-01-01", "2020-01-01",
             "pending", [(1, "第一条 测试。")])
    # G2：同一部法两个现行版本；G3：同日生效
    _version(conn, "实验法", "实验法", "2020-01-01", "2021-01-01",
             "superseded", [(1, "第一条 实验。")])
    _version(conn, "实验法", "实验法", "2026-01-01", "2021-01-01",
             "effective", [(1, "第一条 实验，修订版。")])
    _version(conn, "实验法", "实验法", "2027-01-01", "2026-02-01",
             "effective", [(1, "第一条 实验，第三次修订。")])
    # G4：Word 域代码 + 页脚 CTA
    _version(conn, "域代码测试法", "域代码测试法", "2020-01-01", "2020-07-01",
             "effective", [(1, "第一条 正文。\nPAGE  \\* MERGEFORMAT - 3 -\n"
                                "扫一扫，手机阅读更方便")])
    # G6：同名两版条文哈希完全一致
    _version(conn, "同文测试法", "同文测试法", "2020-01-01", "2020-07-01",
             "superseded", [(1, "第一条 同样的话。")])
    _version(conn, "同文测试法", "同文测试法", "2024-01-01", "2024-07-01",
             "effective", [(1, "第一条 同样的话。")])
    # G7：0 条版本 / 单条跳号 / 超长条文
    _version(conn, "空条文测试法", "空条文测试法", "2020-01-01", "2020-07-01",
             "effective", [])
    _version(conn, "跳号测试法", "跳号测试法", "2020-01-01", "2020-07-01",
             "effective", [(344, "第三百四十四条 只有一条。")])
    _version(conn, "超长条文测试法", "超长条文测试法", "2020-01-01", "2020-07-01",
             "effective", [(1, "第一条 " + "甲" * 6000)])
    conn.commit()
    conn.close()


def counts(db_path):
    conn = sqlite3.connect(db_path)
    articles = conn.execute(
        "SELECT COUNT(*) FROM law_articles WHERE status != 'repealed'").fetchone()[0]
    laws = conn.execute(
        "SELECT COUNT(DISTINCT norm_name) FROM law_versions WHERE status='effective'").fetchone()[0]
    conn.close()
    return articles, laws


def write_manifest(path, db_path, artifact_path, *, app_version=APP_VERSION,
                   corpus_version="2026.11.05.1", tamper=False):
    articles, laws = counts(db_path)
    payload = {
        "schema": 1,
        "schema_version": 3,
        "app_version": app_version,
        "corpus_version": corpus_version,
        "law_count": laws + (1 if tamper else 0),
        "article_count": articles + (1 if tamper else 0),
        "db_sha256": "0" * 64 if tamper else _sha256(db_path),
        "artifacts": [{
            "name": os.path.basename(artifact_path),
            "sha256": "0" * 64 if tamper else _sha256(artifact_path),
            "size": 1 if tamper else os.path.getsize(artifact_path),
            "kind": "full",
        }],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def run_gate(db_path, manifest, version_file, artifacts_dir, catalog, plans_dir):
    cmd = [sys.executable, os.path.join(_HERE, "release_gate.py"),
           "--db", db_path, "--manifest", manifest, "--version-file", version_file,
           "--artifacts-dir", artifacts_dir, "--catalog", catalog,
           "--plans-dir", plans_dir, "--today", TODAY]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def statuses(stdout):
    out = {}
    for line in stdout.splitlines():
        if line.startswith("[G") and "]" in line:
            gate = line[1:3]
            if "阻断" in line:
                out[gate] = "FAIL"
            elif "跳过" in line:
                out[gate] = "SKIP"
            elif "告警" in line:
                out[gate] = "WARN"
            elif "通过" in line:
                out[gate] = "PASS"
    return out


def main() -> int:
    workdir = tempfile.mkdtemp(prefix="gate-selftest-")
    try:
        good_db = os.path.join(workdir, "good.db")
        bad_db = os.path.join(workdir, "bad.db")
        build_good(good_db, "2026.11.05.1")
        build_bad(bad_db, "2026.11.05.1")

        version_file = os.path.join(workdir, "VERSION")
        with open(version_file, "w", encoding="utf-8") as f:
            f.write(APP_VERSION + "\n")

        artifact = os.path.join(workdir, "law_data-2026.11.05.1.db")
        with open(artifact, "wb") as f:
            f.write(b"fake-release-artifact")

        good_manifest = os.path.join(workdir, "manifest-good.json")
        bad_manifest = os.path.join(workdir, "manifest-bad.json")
        write_manifest(good_manifest, good_db, artifact)
        write_manifest(bad_manifest, bad_db, artifact, app_version="2.3.3",
                       corpus_version="2026.11.03.3", tamper=True)

        good_catalog = os.path.join(workdir, "catalog-good.csv")
        with open(good_catalog, "w", encoding="utf-8") as f:
            f.write("norm_name,latest_publish_date\n劳动合同法,2025-01-01\n社会保险法,2026-09-01\n")

        bad_catalog = os.path.join(workdir, "catalog-bad.csv")
        with open(bad_catalog, "w", encoding="utf-8") as f:
            f.write("norm_name,latest_publish_date\n劳动合同法,2025-01-02\n民法典,2021-01-01\n")

        good_plans = os.path.join(workdir, "plans-good")
        bad_plans = os.path.join(workdir, "plans-bad")
        os.makedirs(good_plans)
        os.makedirs(bad_plans)
        with open(os.path.join(bad_plans, "empty-increment.plan.json"), "w", encoding="utf-8") as f:
            json.dump({"schema": 1, "summary": {"new_laws": 0, "skipped_laws": 3}}, f)

        print("=== 干净库：期望全部通过 ===")
        code, out = run_gate(good_db, good_manifest, version_file, workdir, good_catalog, good_plans)
        print(out)
        got = statuses(out)
        expect_good = {g: "PASS" for g in ("G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9")}
        ok = code == 0 and all(got.get(g) == v for g, v in expect_good.items())

        print("=== 坏样本库：期望全部拦下（G6 仅告警） ===")
        code_bad, out_bad = run_gate(bad_db, bad_manifest, version_file, workdir,
                                     bad_catalog, bad_plans)
        print(out_bad)
        got_bad = statuses(out_bad)
        expect_bad = {"G1": "FAIL", "G2": "FAIL", "G3": "FAIL", "G4": "FAIL",
                      "G5": "FAIL", "G6": "WARN", "G7": "FAIL", "G8": "FAIL", "G9": "FAIL"}
        ok_bad = code_bad == 1 and all(got_bad.get(g) == v for g, v in expect_bad.items())

        print("=== 断言 ===")
        print(f"干净库退出码 {code}（期望 0）：{'OK' if code == 0 else 'NG'}")
        print(f"坏样本退出码 {code_bad}（期望 1）：{'OK' if code_bad == 1 else 'NG'}")
        for gate, expected in expect_good.items():
            actual = got.get(gate)
            print(f"  {gate} 干净={actual:<4} 期望={expected:<4} {'OK' if actual == expected else 'NG'}")
        for gate, expected in expect_bad.items():
            actual = got_bad.get(gate)
            print(f"  {gate} 坏样本={actual:<4} 期望={expected:<4} "
                  f"{'OK' if actual == expected else 'NG'}")
        return 0 if (ok and ok_bad) else 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
