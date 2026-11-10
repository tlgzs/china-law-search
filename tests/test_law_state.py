"""法条状态推导回归用例（law_state 公共层）。

重点回归「法规更新后，旧条号被报成整部法规废止」这一类信任问题：
  1. 现行版仍有的条号 → in_force / citation_valid=true，并给出所属版本；
  2. 现行版删掉的条号 → removed_in_current，返回旧版原文，绝不出现「该法规已废止」；
  3. 整部法规真废止 → repealed，带废止日期与依据；
  4. 库里没有现行版 → unknown，不得冒充 repealed；
  5. 按时间点查询 → 当时有效，并说明何时被哪一版取代；
  6. 条号调整 → renumbered，且只在内容高度相近时才报。

用例全部跑在临时空库上，不碰 data/law_data.db。

运行：python3 -m unittest tests.test_law_state -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest import schema as S  # noqa: E402
from server import law_state  # noqa: E402
from server.law_query_core import LawStore  # noqa: E402


class LawStateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        S.ensure_schema(self.conn)
        self.store = LawStore(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        os.unlink(self.db_path)

    # ---------- 造数工具 ----------

    def _version(self, vid: int, law_name: str, publish: str, effective: str,
                 status: str = "effective") -> None:
        self.conn.execute(
            "INSERT INTO law_versions(id, law_name, norm_name, law_type, law_type_label, "
            "publish_date, effective_date, source_file, article_count, status, batch_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (vid, law_name, law_name, "local_regulation", "地方性法规",
             publish, effective, "test.md", 0, status, "test-batch", "2026-01-01T00:00:00Z"),
        )

    def _article(self, vid: int, law_name: str, num: int, content: str,
                 status: str = "effective", repeal_date: str = "",
                 repeal_reason: str = "", effective: str = "") -> None:
        self.conn.execute(
            "INSERT INTO law_articles(version_id, law_name, law_type, law_type_label, "
            "article_number, article_number_num, article_suffix, article, content, "
            "publish_date, effective_date, status, repeal_date, repeal_reason, source_file) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (vid, law_name, "local_regulation", "地方性法规",
             f"第{num}条", num, "", f"第{num}条", content,
             "", effective, status, repeal_date, repeal_reason, "test.md"),
        )

    def _amended_law(self) -> str:
        """三版法规：2025 年修订删除第 27 条（下位例同江苏条例）。"""
        name = "测试省条例"
        self._version(1, name, "2003-06-24", "2003-09-01", "superseded")
        self._version(2, name, "2018-03-28", "2018-05-01", "superseded")
        self._version(3, name, "2025-05-30", "2025-07-01", "effective")
        for vid in (1, 2):
            self._article(vid, name, 1, "第一条内容", status="repealed",
                          repeal_date="2025-05-30", repeal_reason="被新修订版本取代")
            self._article(vid, name, 27, "旧第二十七条内容", status="repealed",
                          repeal_date="2025-05-30", repeal_reason="被新修订版本取代")
        self._article(3, name, 1, "第一条内容")
        self._article(3, name, 2, "第二条内容")
        self.conn.commit()
        return name

    # ---------- 1. 现行条号 ----------

    def test_in_force_article_reports_version(self):
        name = self._amended_law()
        result = self.store.get_article(name, 1)

        self.assertEqual(result["article_status"], law_state.ARTICLE_IN_FORCE)
        self.assertTrue(result["citation_valid"])
        self.assertEqual(result["law_status"], law_state.LAW_EFFECTIVE)
        self.assertEqual(result["version"]["version_id"], 3)
        self.assertTrue(result["version"]["is_current"])
        self.assertEqual(result["status"], "effective")
        self.assertEqual(result["article_number"], "第1条")

    # ---------- 2. 现行版删除的条号 ----------

    def test_article_removed_in_current_keeps_old_text_and_never_says_law_repealed(self):
        name = self._amended_law()
        result = self.store.get_article(name, 27)

        self.assertEqual(result["article_status"], law_state.ARTICLE_REMOVED)
        self.assertFalse(result["citation_valid"])
        self.assertEqual(result["law_status"], law_state.LAW_EFFECTIVE)
        self.assertEqual(result["status"], "removed")
        self.assertFalse(result["is_repealed"])
        self.assertEqual(result["previous_content"], "旧第二十七条内容")
        self.assertEqual(result["previous_version"]["version_id"], 2)
        self.assertIn("已无本条", result["message"])
        self.assertNotIn("废止", result["message"])

    # ---------- 3. 条号调整 ----------

    def test_renumbered_only_when_content_matches(self):
        name = "测试改号条例"
        self._version(10, name, "2020-01-01", "2020-02-01", "superseded")
        self._version(11, name, "2024-01-01", "2024-02-01", "effective")
        self._article(10, name, 18, "本条规定的具体内容完全相同。", status="repealed",
                      repeal_date="2024-02-01", repeal_reason="被新修订版本取代")
        self._article(11, name, 19, "本条规定的具体内容完全相同。")
        self.conn.commit()

        result = self.store.get_article(name, 18)

        self.assertEqual(result["article_status"], law_state.ARTICLE_RENUMBERED)
        self.assertFalse(result["citation_valid"])
        self.assertTrue(result["renumber_candidates"])
        self.assertEqual(result["renumber_candidates"][0]["article_number"], "第19条")

    # ---------- 4. 整部法规真废止 ----------

    def test_repealed_law_reports_repeal_date(self):
        name = "测试废止条例"
        self._version(20, name, "2010-01-01", "2010-02-01", "superseded")
        self._article(20, name, 1, "第一条内容", status="repealed",
                      repeal_date="2024-01-01",
                      repeal_reason="被《关于废止测试废止条例的决定》废止")
        self.conn.commit()

        result = self.store.get_article(name, 1)

        self.assertEqual(result["law_status"], law_state.LAW_REPEALED)
        self.assertEqual(result["article_status"], law_state.ARTICLE_REPEALED)
        self.assertTrue(result["is_repealed"])
        self.assertFalse(result["citation_valid"])
        self.assertEqual(result["repeal_date"], "2024-01-01")
        self.assertIn("废止", result["message"])

    # ---------- 5. 无现行版 ----------

    def test_missing_current_version_is_unknown_not_repealed(self):
        name = "测试缺版条例"
        self._version(30, name, "2019-01-01", "2019-02-01", "superseded")
        self._article(30, name, 1, "第一条内容", status="repealed",
                      repeal_date="2022-01-01", repeal_reason="被新修订版本取代")
        self.conn.commit()

        result = self.store.get_article(name, 1)

        self.assertEqual(result["law_status"], law_state.LAW_NO_CURRENT)
        self.assertEqual(result["article_status"], law_state.ARTICLE_UNKNOWN)
        self.assertFalse(result["is_repealed"])
        self.assertFalse(result["citation_valid"])
        self.assertIn("未收录现行版", result["message"])
        self.assertNotIn("废止", result["message"])

    # ---------- 6. 按时间点查询 ----------

    def test_as_of_returns_text_in_force_then_and_says_when_superseded(self):
        name = self._amended_law()

        result = self.store.get_article(name, 1, as_of="2015-01-01")

        self.assertEqual(result["article_status"], law_state.ARTICLE_IN_FORCE)
        self.assertFalse(result["citation_valid"])
        self.assertEqual(result["status"], "superseded")
        self.assertTrue(result["historical"])
        self.assertEqual(result["version"]["version_id"], 1)
        self.assertEqual(result["superseded_date"], "2018-05-01")
        self.assertIn("2015-01-01", result["message"])
        self.assertIn("取代", result["message"])
        self.assertNotIn("废止", result["message"])

    def test_as_of_removed_article_points_to_current_version(self):
        name = self._amended_law()

        result = self.store.get_article(name, 27, as_of="2015-01-01")

        self.assertEqual(result["article_status"], law_state.ARTICLE_IN_FORCE)
        self.assertEqual(result["content"], "旧第二十七条内容")
        self.assertIn("现行版中已无本条", result["message"])
        self.assertFalse(result["citation_valid"])

    def test_as_of_before_first_version_is_structured_error(self):
        name = self._amended_law()

        result = self.store.get_article(name, 1, as_of="2003-01-01")

        self.assertEqual(result["error"], "no_version_in_force")
        self.assertIn("尚无施行版本", result["message"])
        self.assertTrue(result["available_versions"])

    # ---------- 7. 已公布未生效 ----------

    def test_pending_version_is_not_citable(self):
        name = "测试待生效条例"
        self._version(40, name, "2020-01-01", "2020-02-01", "effective")
        self._version(41, name, "2026-01-01", "2027-01-01", "pending")
        self._article(41, name, 5, "待生效条文内容")
        self.conn.commit()

        result = self.store.get_article(name, 5)

        self.assertEqual(result["article_status"], law_state.ARTICLE_IN_FORCE)
        self.assertFalse(result["citation_valid"])
        self.assertTrue(result["is_pending"])
        self.assertIn("尚未生效", result["message"])

    # ---------- 8. 核验入口与兼容字段 ----------

    def test_verify_distinguishes_removed_from_not_found(self):
        name = self._amended_law()

        data = self.store.verify([f"{name}第一条", f"{name}第二十七条"])
        current, removed = data["results"]

        self.assertTrue(current["verified"])
        self.assertEqual(current["article_status"], law_state.ARTICLE_IN_FORCE)
        self.assertTrue(current["citation_valid"])

        self.assertFalse(removed["verified"])
        self.assertEqual(removed["article_status"], law_state.ARTICLE_REMOVED)
        self.assertEqual(removed["status"], "removed")
        self.assertEqual(removed["error"], "现行版已删除该条")
        self.assertFalse(removed["citation_valid"])
        self.assertEqual(removed["previous_content"], "旧第二十七条内容")
        self.assertNotIn("该法规已废止", removed["warning"] or "")

    def test_not_found_article_stays_not_found(self):
        name = self._amended_law()

        result = self.store.get_article(name, 99)

        self.assertEqual(result["error"], "not_found")


if __name__ == "__main__":
    unittest.main()
