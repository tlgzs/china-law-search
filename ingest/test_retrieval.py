"""
法律法规检索测试脚本
====================
测试向量库的检索效果。

测试内容:
1. 语义搜索 (向量相似度)
2. 按法规类型过滤
3. 按法规名称过滤
4. 精确条号查找
5. 多条件组合查询

用法:
    python test_retrieval.py
"""

import os
import sys
import time
import argparse
from typing import List, Dict


def init_vectorstore(
    db_path: str,
    collection_name: str,
    embedding_model: str,
):
    """初始化向量库 (复用 ingest_to_vectordb 的逻辑)"""
    from langchain_chroma import Chroma
    from langchain_huggingface import HuggingFaceEmbeddings

    embeddings = HuggingFaceEmbeddings(
        model_name=embedding_model,
        model_kwargs={"device": "cpu"},  # 检索时用 CPU 即可
    )

    vectorstore = Chroma(
        collection_name=collection_name,
        embedding_function=embeddings,
        persist_directory=db_path,
    )

    return vectorstore


def format_result(doc, score=None) -> str:
    """格式化单条检索结果"""
    meta = doc.metadata
    lines = []
    lines.append(f"《{meta.get('law_name', '?')}》")
    lines.append(f"  {meta.get('article_number', '?')} "
                 f"(第{meta.get('article_number_num', '?')}条)")
    if meta.get("chapter"):
        lines.append(f"  章节: {meta['chapter']}")
    if meta.get("section"):
        lines.append(f"  节: {meta['section']}")
    lines.append(f"  正文: {meta.get('content', doc.page_content)[:100]}...")
    lines.append(f"  类型: {meta.get('law_type_label', '?')}")
    if meta.get("effective_date"):
        lines.append(f"  施行: {meta.get('effective_date')}")
    if score is not None:
        lines.append(f"  相似度: {score:.4f}")
    return "\n".join(lines)


def test_semantic_search(vectorstore, query: str, k: int = 5):
    """测试1: 语义搜索"""
    print(f"\n{'='*60}")
    print(f"测试1: 语义搜索")
    print(f"查询: '{query}'")
    print(f"{'='*60}")

    start = time.time()
    results = vectorstore.similarity_search_with_score(query, k=k)
    elapsed = time.time() - start

    for doc, score in results:
        print(format_result(doc, score))
        print()

    print(f"耗时: {elapsed*1000:.0f}ms")
    return results


def test_filter_by_type(vectorstore, query: str, law_type: str, k: int = 5):
    """测试2: 按法规类型过滤"""
    print(f"\n{'='*60}")
    print(f"测试2: 按法规类型过滤")
    print(f"查询: '{query}'")
    print(f"过滤: law_type = '{law_type}'")
    print(f"{'='*60}")

    start = time.time()
    results = vectorstore.similarity_search_with_score(
        query, k=k, filter={"law_type": law_type}
    )
    elapsed = time.time() - start

    for doc, score in results:
        print(format_result(doc, score))
        print()

    print(f"耗时: {elapsed*1000:.0f}ms")
    return results


def test_filter_by_law(vectorstore, query: str, law_name: str, k: int = 5):
    """测试3: 按法规名称过滤"""
    print(f"\n{'='*60}")
    print(f"测试3: 按法规名称过滤")
    print(f"查询: '{query}'")
    print(f"过滤: law_name = '{law_name}'")
    print(f"{'='*60}")

    start = time.time()
    results = vectorstore.similarity_search_with_score(
        query, k=k, filter={"law_name": law_name}
    )
    elapsed = time.time() - start

    for doc, score in results:
        print(format_result(doc, score))
        print()

    print(f"耗时: {elapsed*1000:.0f}ms")
    return results


def test_exact_article(vectorstore, law_name: str, article_num: int):
    """测试4: 精确条号查找"""
    print(f"\n{'='*60}")
    print(f"测试4: 精确条号查找")
    print(f"查找: 《{law_name}》第{article_num}条")
    print(f"{'='*60}")

    start = time.time()
    # 用查询语句搜索 (会先用 embedding 搜索,再用 metadata 过滤)
    query = f"{law_name} 第{article_num}条"
    results = vectorstore.similarity_search_with_score(
        query, k=10, filter={
            "$and": [
                {"law_name": law_name},
                {"article_number_num": article_num},
            ]
        }
    )
    elapsed = time.time() - start

    if results:
        doc, score = results[0]
        print(format_result(doc, score))
    else:
        print("未找到匹配结果")

    print(f"\n耗时: {elapsed*1000:.0f}ms")
    return results


def test_combined_query(
    vectorstore, query: str, law_type: str, k: int = 5
):
    """测试5: 多条件组合查询"""
    print(f"\n{'='*60}")
    print(f"测试5: 多条件组合查询")
    print(f"查询: '{query}'")
    print(f"过滤: law_type = '{law_type}'")
    print(f"{'='*60}")

    start = time.time()
    results = vectorstore.similarity_search_with_score(
        query, k=k, filter={"law_type": law_type}
    )
    elapsed = time.time() - start

    for doc, score in results:
        print(format_result(doc, score))
        print()

    print(f"耗时: {elapsed*1000:.0f}ms")
    return results


def test_trial_scenario(vectorstore):
    """测试6: 模拟法庭场景 - 根据案件要素检索相关法条"""
    print(f"\n{'='*60}")
    print(f"测试6: 模拟法庭场景")
    print(f"{'='*60}")

    # 模拟案件要素
    scenarios = [
        {
            "name": "民间借贷纠纷 - 利率上限",
            "query": "民间借贷利率超过法定上限 高利贷",
            "expected_law": "民法典",
        },
        {
            "name": "交通事故责任 - 赔偿",
            "query": "机动车交通事故人身损害赔偿 责任认定",
            "expected_law": "民法典",
        },
        {
            "name": "劳动争议 - 解除劳动合同",
            "query": "用人单位违法解除劳动合同 经济补偿金",
            "expected_law": "劳动合同法",
        },
        {
            "name": "民间借贷司法解释 - 24%利率",
            "query": "民间借贷年利率24% 36% 司法保护上限",
            "expected_law": "民间借贷",
        },
    ]

    for scenario in scenarios:
        print(f"\n--- {scenario['name']} ---")
        print(f"查询: '{scenario['query']}'")
        print(f"期望找到: 《{scenario['expected_law']}》")

        start = time.time()
        results = vectorstore.similarity_search_with_score(
            scenario["query"], k=3
        )
        elapsed = time.time() - start

        for doc, score in results[:3]:
            meta = doc.metadata
            print(f"  《{meta.get('law_name', '?')}》 "
                  f"{meta.get('article_number', '?')} "
                  f"(相似度: {score:.4f})")
            print(f"    {meta.get('content', '')[:60]}...")

        print(f"  耗时: {elapsed*1000:.0f}ms")


def run_all_tests(vectorstore):
    """运行所有测试"""
    print("=" * 60)
    print("法律法规检索测试")
    print("=" * 60)

    # 检查数据量
    try:
        count = vectorstore._collection.count()
        print(f"向量库记录数: {count}")
    except Exception:
        pass

    # 测试1: 语义搜索
    test_semantic_search(
        vectorstore,
        query="禁止高利放贷 借款利率",
        k=5,
    )

    # 测试2: 按法规类型过滤
    test_filter_by_type(
        vectorstore,
        query="借款合同 利率",
        law_type="law",
        k=5,
    )

    # 测试3: 按法规名称过滤
    test_filter_by_law(
        vectorstore,
        query="违约责任",
        law_name="中华人民共和国民法典",
        k=5,
    )

    # 测试4: 精确条号查找
    test_exact_article(
        vectorstore,
        law_name="中华人民共和国民法典",
        article_num=680,
    )

    # 测试5: 多条件组合
    test_combined_query(
        vectorstore,
        query="劳动合同 解除 经济补偿",
        law_type="law",
        k=5,
    )

    # 测试6: 模拟法庭场景
    test_trial_scenario(vectorstore)

    print(f"\n{'='*60}")
    print("所有测试完成!")
    print(f"{'='*60}")


# ============================================================
# 命令行入口
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="法律法规检索测试")
    parser.add_argument(
        "--db-path",
        default="./data/legal_vector_db",
        help="ChromaDB 路径 (默认: ./data/legal_vector_db)",
    )
    parser.add_argument(
        "--collection-name",
        default="chinese_laws",
        help="集合名称 (默认: chinese_laws)",
    )
    parser.add_argument(
        "--embedding-model",
        default="BAAI/bge-large-zh-v1.5",
        help="embedding 模型 (默认: BAAI/bge-large-zh-v1.5)",
    )
    args = parser.parse_args()

    vectorstore = init_vectorstore(
        db_path=args.db_path,
        collection_name=args.collection_name,
        embedding_model=args.embedding_model,
    )

    run_all_tests(vectorstore)
