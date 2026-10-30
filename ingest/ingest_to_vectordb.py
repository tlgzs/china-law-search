"""
法律法规向量化导入脚本
======================
把所有 Markdown 法规文件解析后，逐条导入 ChromaDB 向量库。

每条法条 = 一个向量 + 完整 metadata:
  - law_name: 法规名称
  - law_type: 法规类型 (law/regulation/local_regulation/...)
  - law_type_label: 类型中文标签
  - article_number: 条号 (如 "第六百八十条")
  - article_number_num: 条号数字 (如 680)
  - chapter: 所属章
  - section: 所属节
  - content: 法条正文 (同时作为 embedding 输入)
  - publish_date: 公布日期
  - effective_date: 施行日期
  - issuing_authority: 制定机关
  - status: 状态 (effective)
  - source_file: 来源文件

用法:
    # 在 fmzy 上运行 (需要 GPU)
    python ingest_to_vectordb.py --batch-size 200

    # CPU 模式 (很慢, 仅测试用)
    python ingest_to_vectordb.py --cpu --limit 100

    # 增量导入 (跳过已处理的文件)
    python ingest_to_vectordb.py --resume
"""

import os
import sys
import json
import time
import argparse
from typing import List, Dict, Optional

# 添加解析器路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

from parse_markdown import parse_markdown_file, parse_directory, ParseResult


# ============================================================
# ChromaDB 初始化
# ============================================================

def init_vectorstore(
    db_path: str,
    collection_name: str,
    embedding_model: str,
    use_cpu: bool = False,
):
    """
    初始化 ChromaDB 向量库
    
    Args:
        db_path: ChromaDB 持久化路径
        collection_name: 集合名称
        embedding_model: embedding 模型名称
        use_cpu: 是否强制使用 CPU
    
    Returns:
        vectorstore: ChromaDB 向量库实例
    """
    from langchain_chroma import Chroma
    from langchain_huggingface import HuggingFaceEmbeddings

    # 检测 GPU
    try:
        import torch
        cuda_available = torch.cuda.is_available()
        if cuda_available:
            gpu_name = torch.cuda.get_device_name(0)
            print(f"GPU 检测: {gpu_name}")
        else:
            print("GPU 检测: 未检测到 CUDA, 使用 CPU")
    except ImportError:
        cuda_available = False
        print("torch 未安装, 使用 CPU")

    # 设置设备
    device = "cpu" if use_cpu or not cuda_available else "cuda"

    # 初始化 embedding 模型
    print(f"加载 embedding 模型: {embedding_model}")
    print(f"设备: {device}")
    
    embeddings = HuggingFaceEmbeddings(
        model_name=embedding_model,
        model_kwargs={"device": device},
        encode_kwargs={"batch_size": 32},
    )

    # 初始化 ChromaDB
    os.makedirs(db_path, exist_ok=True)
    vectorstore = Chroma(
        collection_name=collection_name,
        embedding_function=embeddings,
        persist_directory=db_path,
    )

    # 检查现有数据量
    try:
        count = vectorstore._collection.count()
        print(f"向量库现有记录: {count} 条")
    except Exception:
        pass

    return vectorstore


# ============================================================
# 法条转 Document
# ============================================================

def articles_to_documents(result: ParseResult) -> List[Dict]:
    """
    把解析结果转为 Document 列表
    
    每条法条包含:
    - page_content: 法条正文 (用于生成 embedding)
    - metadata: 完整法条信息
    """
    from langchain_core.documents import Document

    documents = []
    for article in result.articles:
        # 法条正文 + 上下文信息 (帮助语义检索)
        content_parts = [article.content]
        if article.chapter:
            content_parts.append(f"（{article.chapter}）")
        if article.law_name:
            content_parts.append(f"《{article.law_name}》")

        doc = Document(
            page_content="\n".join(content_parts),
            metadata={
                "law_name": article.law_name,
                "law_type": article.law_type,
                "law_type_label": article.law_type_label,
                "article_number": article.article_number,
                "article_number_num": article.article_number_num,
                "chapter": article.chapter,
                "section": article.section,
                "article": article.content,
                "content": article.content,
                "publish_date": article.publish_date,
                "effective_date": article.effective_date,
                "issuing_authority": article.issuing_authority,
                "status": article.status,
                "source_file": article.source_file,
            },
        )
        documents.append(doc)

    return documents


# ============================================================
# 批量导入
# ============================================================

def ingest_directory(
    vectorstore,
    input_dirs: List[tuple],  # [(dir_path, dir_label), ...]
    batch_size: int = 100,
    resume: bool = False,
    progress_log: str = None,
    file_limit: Optional[int] = None,
) -> dict:
    """
    批量导入法条到向量库
    
    Args:
        vectorstore: ChromaDB 实例
        input_dirs: 输入目录列表 [(dir_path, dir_label), ...]
        batch_size: 每批写入多少条法条
        resume: 是否断点续传
        progress_log: 进度日志路径
        file_limit: 最多处理多少个文件 (None=全部)
    
    Returns:
        dict: 统计信息
    """
    # 加载已处理文件列表 (断点续传)
    processed_files = set()
    if resume and progress_log and os.path.exists(progress_log):
        with open(progress_log, "r") as f:
            for line in f:
                processed_files.add(line.strip())
        print(f"断点续传: 已处理 {len(processed_files)} 个文件")

    stats = {
        "total_files": 0,
        "processed_files": 0,
        "skipped_files": 0,
        "total_articles": 0,
        "errors": [],
        "start_time": time.time(),
    }

    # 收集所有文件
    all_files = []
    for dir_path, dir_label in input_dirs:
        if not os.path.exists(dir_path):
            print(f"[跳过] 目录不存在: {dir_path}")
            continue
        for root, dirs, files in os.walk(dir_path):
            for f in files:
                if f.endswith(".md"):
                    filepath = os.path.join(root, f)
                    if resume and filepath in processed_files:
                        stats["skipped_files"] += 1
                        continue
                    all_files.append((filepath, dir_label))

    all_files.sort()
    if file_limit and file_limit > 0:
        all_files = all_files[:file_limit]
    stats["total_files"] = len(all_files) + stats["skipped_files"]
    print(f"待处理文件: {len(all_files)} 个 (跳过 {stats['skipped_files']} 个)")
    print(f"批次大小: {batch_size}")
    print()

    # 批量处理
    batch_documents = []
    batch_filepaths = []
    progress_file = open(progress_log, "a") if progress_log else None

    def write_progress(filepaths: List[str]):
        if not progress_file:
            return
        for processed_path in filepaths:
            progress_file.write(processed_path + "\n")
        progress_file.flush()

    try:
        for i, (filepath, dir_label) in enumerate(all_files):
            # 解析文件
            result = parse_markdown_file(filepath, dir_label=dir_label)

            if result.parse_errors:
                stats["errors"].extend([
                    {"file": result.source_file, "error": e}
                    for e in result.parse_errors
                ])

            # 转为 Document
            docs = articles_to_documents(result)
            batch_documents.extend(docs)
            if docs:
                batch_filepaths.append(filepath)
            else:
                write_progress([filepath])
            stats["total_articles"] += len(docs)
            stats["processed_files"] += 1

            # 批量写入
            if len(batch_documents) >= batch_size:
                try:
                    vectorstore.add_documents(batch_documents)
                    write_progress(batch_filepaths)
                    batch_documents = []
                    batch_filepaths = []
                except Exception as e:
                    stats["errors"].append({
                        "file": result.source_file,
                        "error": f"写入失败: {e}",
                    })
                    batch_documents = []
                    batch_filepaths = []

            # 进度显示
            done = i + 1
            if done % 100 == 0 or done == len(all_files):
                elapsed = time.time() - stats["start_time"]
                rate = done / elapsed if elapsed > 0 else 0
                eta = (len(all_files) - done) / rate if rate > 0 else 0
                print(f"  进度: {done}/{len(all_files)} "
                      f"({done/len(all_files)*100:.1f}%) "
                      f"文件:{stats['processed_files']} "
                      f"法条:{stats['total_articles']} "
                      f"速度:{rate:.1f}文件/秒 "
                      f"剩余:{eta:.0f}秒")

        # 写入剩余的法条
        if batch_documents:
            try:
                vectorstore.add_documents(batch_documents)
                write_progress(batch_filepaths)
            except Exception as e:
                stats["errors"].append({
                    "file": "final_batch",
                    "error": f"最终批次写入失败: {e}",
                })

    finally:
        if progress_file:
            progress_file.close()

    stats["elapsed"] = time.time() - stats["start_time"]
    return stats


# ============================================================
# 命令行入口
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="法律法规向量化导入")
    parser.add_argument(
        "--db-path",
        default="./data/legal_vector_db",
        help="ChromaDB 持久化路径 (默认: ./data/legal_vector_db)",
    )
    parser.add_argument(
        "--collection-name",
        default="chinese_laws",
        help="ChromaDB 集合名称 (默认: chinese_laws)",
    )
    parser.add_argument(
        "--embedding-model",
        default="BAAI/bge-large-zh-v1.5",
        help="embedding 模型 (默认: BAAI/bge-large-zh-v1.5)",
    )
    parser.add_argument(
        "--batch-size", type=int, default=100,
        help="每批写入多少条法条 (默认: 100)",
    )
    parser.add_argument(
        "--cpu", action="store_true",
        help="强制使用 CPU (很慢)",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="最多处理多少个文件 (0=全部)",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="断点续传 (跳过已处理的文件)",
    )
    parser.add_argument(
        "--progress-log",
        default="./ingest_progress.log",
        help="进度日志路径 (用于断点续传)",
    )
    args = parser.parse_args()

    # 默认路径
    cwd = os.getcwd()
    LAW_BASE = os.path.join(cwd, "法律法规库")
    if not os.path.exists(LAW_BASE):
        # 尝试项目根目录
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.dirname(SCRIPT_DIR))
        )))
        LAW_BASE = os.path.join(project_root, "法律法规库")

    # 输入目录
    input_dirs = [
        (os.path.join(LAW_BASE, "法律_markdown"), "法律"),
        (os.path.join(LAW_BASE, "行政法规_markdown"), "行政法规"),
        (os.path.join(LAW_BASE, "司法解释_markdown"), "司法解释"),
        (os.path.join(LAW_BASE, "监察法规_markdown"), "监察法规"),
        (os.path.join(LAW_BASE, "地方法规_markdown"), "地方法规"),
        (os.path.join(LAW_BASE, "宪法_markdown"), "宪法"),
    ]

    # 限制文件数 (测试用)
    if args.limit > 0:
        limited_dirs = []
        for dir_path, dir_label in input_dirs:
            if os.path.exists(dir_path):
                files = sorted([
                    os.path.join(dir_path, f)
                    for f in os.listdir(dir_path)
                    if f.endswith(".md")
                ])[:args.limit]
                limited_dirs.append((dir_path, dir_label))
                break
        input_dirs = limited_dirs

    # 初始化向量库
    print("=" * 60)
    print("法律法规向量化导入")
    print("=" * 60)
    print(f"数据库路径: {args.db_path}")
    print(f"集合名称: {args.collection_name}")
    print(f"embedding 模型: {args.embedding_model}")
    print(f"断点续传: {'是' if args.resume else '否'}")
    print()

    vectorstore = init_vectorstore(
        db_path=args.db_path,
        collection_name=args.collection_name,
        embedding_model=args.embedding_model,
        use_cpu=args.cpu,
    )

    # 开始导入
    print()
    stats = ingest_directory(
        vectorstore=vectorstore,
        input_dirs=input_dirs,
        batch_size=args.batch_size,
        resume=args.resume,
        progress_log=args.progress_log,
        file_limit=args.limit if args.limit > 0 else None,
    )

    # 输出统计
    print()
    print("=" * 60)
    print("导入完成!")
    print(f"  处理文件: {stats['processed_files']}")
    print(f"  跳过文件: {stats['skipped_files']}")
    print(f"  导入法条: {stats['total_articles']}")
    print(f"  错误数: {len(stats['errors'])}")
    print(f"  耗时: {stats['elapsed']:.1f}秒")
    print(f"  平均速度: {stats['processed_files']/stats['elapsed']:.1f} 文件/秒")

    if stats["errors"]:
        print()
        print(f"错误 ({len(stats['errors'])} 个):")
        for e in stats["errors"][:10]:
            print(f"  - {e['file']}: {e['error'][:80]}")

    # 验证
    try:
        count = vectorstore._collection.count()
        print()
        print(f"向量库总记录数: {count}")
    except Exception:
        pass
