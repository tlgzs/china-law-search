"""G7 门禁口径回归：source_gap 降告警、parse_gap 阻断、amendment 退出统计。

每个用例在临时库上跑，不碰 data/law_data.db。

运行：python3 -m unittest tests.test_g7_gate_registry -v
"""
from __future__ import annotations

import csv
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.release_gate import FAIL, PASS, WARN, check_g7  # noqa: E402


def _make_db(versions: list[tuple]) -> tuple[sqlite3.Connection, str]:
    """versions: (version_id, law_name, law_type, [条号...], 每条正文汉字数)。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE law_versions (id INTEGER PRIMARY KEY, law_name TEXT, law_type TEXT, "
        "article_count INTEGER, publish_date TEXT)")
    conn.execute(
        "CREATE TABLE law_articles (id INTEGER PRIMARY KEY AUTOINCREMENT, version_id INTEGER, "
        "article_number TEXT, article_number_num INTEGER, article_suffix TEXT, content TEXT)")
    for vid, name, law_type, numbers, content_len in versions:
        conn.execute("INSERT INTO law_versions VALUES (?,?,?,?,?)",
                     (vid, name, law_type, len(numbers), "2020-01-01"))
        for number in numbers:
            conn.execute(
                "INSERT INTO law_articles (version_id, article_number, article_number_num, content) "
                "VALUES (?,?,?,?)", (vid, f"第{number}条", number, "正" * content_len))
    conn.commit()
    return conn, path


def _make_registry(rows: list[dict]) -> str:
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    fields = ["version_id", "law_name", "law_type", "root_cause", "source_file",
              "src_articles", "src_min", "src_max", "missing_ranges", "missing_numbers_total"]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fields})
    return path


class G7GapRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._paths: list[str] = []

    def tearDown(self) -> None:
        for path in self._paths:
            if os.path.exists(path):
                os.remove(path)

    def _db(self, versions: list[tuple]) -> sqlite3.Connection:
        conn, path = _make_db(versions)
        self._paths.append(path)
        return conn

    def _registry(self, rows: list[dict]) -> str:
        path = _make_registry(rows)
        self._paths.append(path)
        return path

    def test_source_gap_downgraded_to_warning(self) -> None:
        conn = self._db([(1, "源缺号法", "local_regulation", [1, 2, 4], 10)])
        registry = self._registry([{
            "version_id": 1, "root_cause": "source_gap", "missing_ranges": "3",
            "missing_numbers_total": 1, "source_file": "源缺号法.md", "src_min": 1, "src_max": 4,
        }])
        result = check_g7(conn, "2026-11-07", registry)
        self.assertEqual(result.status, WARN)
        self.assertEqual(result.stats["source_gap_advisory"], 1)
        self.assertEqual(result.stats["parse_gap_blocking"], 0)
        self.assertIn("source_gap", result.message)

    def test_unregistered_jump_blocks(self) -> None:
        conn = self._db([(1, "未登记法", "local_regulation", [1, 2, 4], 10)])
        registry = self._registry([])
        result = check_g7(conn, "2026-11-07", registry)
        self.assertEqual(result.status, FAIL)
        self.assertEqual(result.stats["parse_gap_blocking"], 1)

    def test_parse_gap_registered_still_blocks(self) -> None:
        conn = self._db([(1, "解析漏切法", "local_regulation", [1, 2, 4], 10)])
        registry = self._registry([{"version_id": 1, "root_cause": "parse_gap"}])
        result = check_g7(conn, "2026-11-07", registry)
        self.assertEqual(result.status, FAIL)

    def test_amendment_excluded_from_jump_stats(self) -> None:
        conn = self._db([(1, "修改决定", "local_regulation", [1, 2, 10], 10)])
        registry = self._registry([{"version_id": 1, "root_cause": "amendment"}])
        result = check_g7(conn, "2026-11-07", registry)
        self.assertEqual(result.status, PASS)
        self.assertEqual(result.stats["jump_rate_violations"], 0)

    def test_amendment_excluded_from_long_article_check(self) -> None:
        conn = self._db([(1, "修改决定", "local_regulation", [1], 6000)])
        registry = self._registry([{"version_id": 1, "root_cause": "amendment"}])
        result = check_g7(conn, "2026-11-07", registry)
        self.assertEqual(result.status, PASS)
        self.assertEqual(result.stats["long_articles"], 0)

    def test_long_article_still_blocks_normal_version(self) -> None:
        conn = self._db([(1, "超长条文法", "local_regulation", [1], 6000)])
        registry = self._registry([])
        result = check_g7(conn, "2026-11-07", registry)
        self.assertEqual(result.status, FAIL)
        self.assertEqual(result.stats["long_articles"], 1)

    def test_source_long_dowgraded_to_warning(self) -> None:
        conn = self._db([(1, "源侧超长法", "local_regulation", [1], 6000)])
        registry = self._registry([{"version_id": 1, "root_cause": "source_long"}])
        result = check_g7(conn, "2026-11-07", registry)
        self.assertEqual(result.status, WARN)
        self.assertEqual(result.stats["long_articles"], 0)
        self.assertEqual(result.stats["source_long_advisory"], 1)


if __name__ == "__main__":
    unittest.main()
