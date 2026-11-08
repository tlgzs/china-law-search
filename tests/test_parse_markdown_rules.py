"""G7 重切前的 5 条解析规则回归用例。

覆盖 ingest/parse_markdown.py 新增的五种切分识别：
  1. U+2028 行分隔符还原为换行
  2. 引用块（>）只在文件头跳过，正文里的引用行保留
  3. **第X条** 加粗条号
  4. [第X条]{.smallcaps} 条号
  5. 句号后同一行的条号

每个用例在临时文件上跑，不碰 data/law_data.db，也不依赖法律法规库目录。

运行：python3 -m unittest tests.test_parse_markdown_rules -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest.parse_markdown import parse_markdown_file  # noqa: E402


class ParseMarkdownRulesTest(unittest.TestCase):
    def _parse(self, body: str):
        fd, path = tempfile.mkstemp(suffix=".md")
        os.close(fd)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        try:
            return parse_markdown_file(path)
        finally:
            os.unlink(path)

    def test_u2028_line_separator(self):
        """U+2028 不还原成换行时，两条会粘成一行、只切出 1 条。"""
        result = self._parse(
            "# 测试条例\n\n第1条　第一条正文。\u2028第2条　第二条正文。\n"
        )
        self.assertEqual(result.article_count, 2)
        self.assertEqual(result.articles[0].content, "第一条正文。")
        self.assertEqual(result.articles[1].content, "第二条正文。")

    def test_quote_block_only_skipped_in_header(self):
        """文件头的 > 是颁布信息（跳过），正文里的 > 是条文内容（保留）。"""
        result = self._parse(
            "# 测试条例\n"
            "> （2026年1月1日某人大常委会通过）\n"
            "第1条　第一条正文。\n"
            "> 引用行属于正文第二段。\n"
        )
        self.assertEqual(result.article_count, 1)
        content = result.articles[0].content
        self.assertEqual(content, "第一条正文。\n引用行属于正文第二段。")
        self.assertNotIn("通过", content)

    def test_bold_article_number(self):
        """**第X条** 形式，含条号单独成行、正文在下一行。"""
        result = self._parse(
            "# 测试条例\n"
            "**第1条** 为了规范管理，制定本条例。\n"
            "**第2条** 本条例适用于本市行政区域。\n"
            "**第3条**\n"
            "本条例自公布之日起施行。\n"
        )
        self.assertEqual(result.article_count, 3)
        self.assertEqual([a.article_number_num for a in result.articles], [1, 2, 3])
        self.assertEqual(result.articles[2].content, "本条例自公布之日起施行。")
        self.assertNotIn("**", result.articles[0].content)

    def test_smallcaps_article_number(self):
        """[第X条]{.smallcaps} 形式。"""
        result = self._parse(
            "# 测试条例\n"
            "[第1条]{.smallcaps}　第一条正文。\n"
            "[第2条]{.smallcaps}　第二条正文。\n"
        )
        self.assertEqual(result.article_count, 2)
        self.assertEqual(result.articles[0].content, "第一条正文。")
        self.assertNotIn("smallcaps", result.articles[0].content)

    def test_article_number_after_period_on_same_line(self):
        """「……。第2条　……」同一行里的下一条要被切开。"""
        result = self._parse(
            "# 测试条例\n"
            "第1条　第一条正文。第2条　第二条正文。\n"
            "第3条　第三条正文。\n"
        )
        self.assertEqual(result.article_count, 3)
        self.assertEqual([a.article_number_num for a in result.articles], [1, 2, 3])
        self.assertEqual(result.articles[0].content, "第一条正文。")
        self.assertEqual(result.articles[1].content, "第二条正文。")

    def test_inline_reference_is_not_split(self):
        """「。第五条规定」是正文引用，条号后没有空白，不能被切成新条。"""
        result = self._parse(
            "# 测试条例\n"
            "第1条　依照本条例第五条规定执行。\n"
        )
        self.assertEqual(result.article_count, 1)
        self.assertEqual(result.articles[0].content, "依照本条例第五条规定执行。")

    def test_plain_file_unchanged(self):
        """没有任何新规则的普通文件保持原样切分。"""
        result = self._parse(
            "# 测试条例\n"
            "## 第一章　总则\n"
            "第1条　第一条正文。\n"
            "第2条　第二条正文。\n"
        )
        self.assertEqual(result.article_count, 2)
        self.assertEqual(result.articles[0].chapter, "第一章　总则")


if __name__ == "__main__":
    unittest.main()
