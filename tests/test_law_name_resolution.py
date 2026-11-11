"""简称/全称归因与整部检索的单测（不依赖 fmzy 实库）。"""

import json
import os
import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1] / "server"

QUAN_MIN_FA_DIAN = {
    "law_name": "中华人民共和国民法典",
    "law_status": "effective",
    "current_version": {
        "version_id": 11,
        "publish_date": "2020-05-28",
        "effective_date": "2021-01-01",
    },
}


def _create_db(path: Path) -> None:
    """构造最小法规库：版本表 + 条文表，只覆盖被测代码用到的列。"""
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE law_versions (
            id INTEGER PRIMARY KEY,
            law_name TEXT,
            norm_name TEXT,
            publish_date TEXT,
            effective_date TEXT,
            status TEXT,
            article_count INTEGER,
            superseded_by INTEGER
        );
        CREATE TABLE law_articles (
            id INTEGER PRIMARY KEY,
            version_id INTEGER,
            article_number TEXT,
            article_number_num INTEGER,
            article_suffix TEXT,
            content TEXT,
            chapter TEXT,
            section TEXT,
            publish_date TEXT,
            effective_date TEXT,
            status TEXT,
            law_type TEXT,
            law_type_label TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO law_versions VALUES (11, ?, ?, ?, ?, ?, 30, NULL)",
        ("中华人民共和国民法典", "民法典", "2020-05-28", "2021-01-01", "effective"),
    )
    for num in range(1, 31):
        conn.execute(
            "INSERT INTO law_articles (version_id, article_number, article_number_num,"
            " article_suffix, content, chapter, section, publish_date, effective_date,"
            " status, law_type, law_type_label)"
            " VALUES (11, ?, ?, '', ?, '', '', '', '', NULL, 'law', '法律')",
            (f"第{num}条", num, f"第{num}条正文。"),
        )
    # 同名歧义样本：两个 law_name 变体共用同一 norm_name，旧版已废止、新版现行
    conn.execute(
        "INSERT INTO law_versions VALUES (21, ?, ?, ?, ?, 'repealed', 10, NULL)",
        (
            "山西省实施《中华人民共和国渔业法》办法",
            "山西省实施中华人民共和国渔业法办法",
            "2010-01-01",
            "2010-01-01",
        ),
    )
    conn.execute(
        "INSERT INTO law_versions VALUES (22, ?, ?, ?, ?, 'effective', 10, NULL)",
        (
            "山西省实施中华人民共和国渔业法办法",
            "山西省实施中华人民共和国渔业法办法",
            "2020-01-01",
            "2020-01-01",
        ),
    )
    conn.commit()
    conn.close()


@pytest.fixture(scope="module")
def law_svc(tmp_path_factory):
    db_path = tmp_path_factory.mktemp("lawdb") / "law_data.db"
    _create_db(db_path)
    os.environ["LAW_DB_PATH"] = str(db_path)
    if str(SERVER_DIR) not in sys.path:
        sys.path.insert(0, str(SERVER_DIR))
    sys.modules.pop("law_search_service_v2", None)
    import law_search_service_v2

    return law_search_service_v2


def test_full_name_hits(law_svc):
    with patch.object(law_svc.law_state, "classify_law", return_value=QUAN_MIN_FA_DIAN):
        result = law_svc._search_title_in_sqlite("中华人民共和国民法典")

    assert result is not None
    assert result["engine"] == "sqlite_title_outline"
    assert result["total"] == 30
    assert result["items"][0]["law_name"] == "中华人民共和国民法典"


def test_short_name_hits(law_svc):
    with patch.object(law_svc.law_state, "classify_law", return_value=QUAN_MIN_FA_DIAN):
        short = law_svc._search_title_in_sqlite("民法典")
        full = law_svc._search_title_in_sqlite("中华人民共和国民法典")

    assert short is not None and full is not None
    assert short["engine"] == full["engine"]
    assert short["total"] == full["total"]


def test_short_name_with_article_number_resolves(law_svc):
    assert law_svc._extract_law_name("《民法典》第二百条") == "民法典"
    assert law_svc._canonical_law_name("民法典") == "中华人民共和国民法典"
    assert law_svc._resolve_matched_law_id("民法典 第二百条") == 11
    assert law_svc._resolve_matched_law_id("中华人民共和国民法典 第二百条") == 11


def test_short_name_not_found(law_svc):
    with patch.object(law_svc.law_state, "classify_law", return_value=None):
        assert law_svc._search_title_in_sqlite("星际资源开发利用管理条例") is None
    assert law_svc._resolve_matched_law_id("星际资源开发利用管理条例") is None


def test_ambiguous_norm_name_is_deterministic(law_svc):
    first = law_svc._resolve_matched_law_id("山西省实施中华人民共和国渔业法办法")
    second = law_svc._resolve_matched_law_id("山西省实施中华人民共和国渔业法办法")

    assert first == 22
    assert second == 22


def test_log_call_writes_matched_law_id(law_svc, tmp_path, monkeypatch):
    log_path = tmp_path / "calls.jsonl"
    monkeypatch.setattr(law_svc, "CALL_LOG_PATH", str(log_path))

    law_svc.log_call(
        client_id="test",
        endpoint="/api/law/search",
        query="民法典 第二百条",
        result_count=1,
        elapsed_ms=3,
    )

    entry = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert entry["matched_law_id"] == 11
    assert entry["query"] == "民法典 第二百条"
