"""增量入库二字倒排索引回归用例。

覆盖 tools/update.py 的实际路径：写版本 → 落条文 → 建 FTS → index_bigram_articles。
回归点：重切前的 _text_grams 调用多传了一个位置参数，增量入库必抛 TypeError，
导致两个字查询静默漏检。用例在临时空库上跑，不碰 data/law_data.db。

运行：python3 -m unittest tests.test_bigram_incremental -v
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest import schema as S  # noqa: E402


class IncrementalBigramIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        S.ensure_schema(self.conn)
        S.meta_set(self.conn, S.BIGRAM_META_KEY, str(S.BIGRAM_INDEX_VERSION))
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        os.unlink(self.db_path)

    def _insert_version_with_articles(self) -> list[int]:
        self.conn.execute(
            "INSERT INTO law_versions(id, law_name, norm_name, law_type, law_type_label, "
            "publish_date, effective_date, source_file, article_count, status, batch_id, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (1, "测试条例", "测试条例", "local_regulation", "地方性法规",
             "2026-01-01", "2026-02-01", "test.md", 2, "effective", "test-batch", "2026-01-01T00:00:00Z"),
        )
        rows = [
            (1, 1, "测试条例", "local_regulation", "地方性法规", "第1条", 1, "",
             "第一条", "为了规范测试工作，制定本条例。", "2026-01-01", "2026-02-01", "test.md"),
            (2, 1, "测试条例", "local_regulation", "地方性法规", "第2条", 2, "",
             "第二条", "本条例自2026年2月1日起施行。", "2026-01-01", "2026-02-01", "test.md"),
        ]
        self.conn.executemany(
            "INSERT INTO law_articles(id, version_id, law_name, law_type, law_type_label, "
            "article_number, article_number_num, article_suffix, article, content, "
            "publish_date, effective_date, source_file) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return [1, 2]

    def test_incremental_ingest_registers_bigrams(self):
        """写版本 → 建 FTS → 增量登记二字倒排，不抛错且能按二字召回。"""
        ids = self._insert_version_with_articles()
        S.create_fts(self.conn)
        self.assertTrue(S.bigram_ready(self.conn))

        written = S.index_bigram_articles(self.conn, ids)
        self.assertGreater(written, 0, "增量登记未写入任何二字片段")
        self.conn.commit()

        n = self.conn.execute("SELECT COUNT(*) FROM law_bigram_add").fetchone()[0]
        self.assertGreater(n, 0, "law_bigram_add 没有记录，2 字查询会漏检")

        # 正文与条文号都要进索引（article 参数不能被丢掉）
        got = set(S.iter_bigram_article_ids(self.conn, "施行"))
        self.assertIn(2, got)
        got2 = set(S.iter_bigram_article_ids(self.conn, "第二"))
        self.assertIn(2, got2, "条文号未进入二字索引")


if __name__ == "__main__":
    unittest.main()
