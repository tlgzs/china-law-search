"""
DOCX/DOC 批量转 Markdown 脚本
===============================
用 pandoc 和 textutil/libreoffice 把地方法规的 Word 文件
批量转换为 Markdown 格式，供解析器使用。

用法:
    python convert_docx_batch.py [--workers 4] [--limit 100]

转换策略:
    - .docx 文件: pandoc 直接转 markdown
    - .doc 文件:
      - macOS: textutil 转 txt (扩展名改为 .md)
      - Linux: libreoffice 转 docx → pandoc 转 markdown
    - 其他格式: 跳过并记录

输出文件名保持与输入一致 (只改扩展名):
    +内蒙古自治区林木种子条例_20200723.docx → +内蒙古自治区林木种子条例_20200723.md
"""

import os
import sys
import time
import shutil
import platform
import argparse
import subprocess
from multiprocessing import Pool, cpu_count
from typing import Optional


# ============================================================
# 单文件转换
# ============================================================

def convert_docx_with_pandoc(input_path: str, output_path: str) -> bool:
    """用 pandoc 把 docx 转成 markdown"""
    try:
        result = subprocess.run(
            [
                "pandoc",
                input_path,
                "-t", "markdown",
                "--wrap=none",
                "-o", output_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def convert_doc_macos(input_path: str, output_path: str) -> bool:
    """macOS: 用 textutil 把 doc 转成 txt"""
    try:
        # textutil 输出 txt 到临时文件,然后重命名
        result = subprocess.run(
            [
                "textutil",
                "-convert", "txt",
                input_path,
                "-output", output_path,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def convert_doc_linux(input_path: str, output_path: str) -> bool:
    """Linux: 用 libreoffice 把 doc 转 docx, 再用 pandoc 转 markdown"""
    import tempfile
    tmp_dir = tempfile.mkdtemp()
    try:
        # libreoffice 转 docx
        result = subprocess.run(
            [
                "libreoffice", "--headless",
                "--convert-to", "docx",
                "--outdir", tmp_dir,
                input_path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return False

        # 找到转换后的 docx
        basename = os.path.splitext(os.path.basename(input_path))[0]
        docx_path = os.path.join(tmp_dir, basename + ".docx")
        if not os.path.exists(docx_path):
            return False

        # pandoc 转 markdown
        return convert_docx_with_pandoc(docx_path, output_path)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def convert_single_file(args: tuple) -> dict:
    """
    转换单个文件 (用于多进程)
    
    Args:
        args: (input_path, output_dir)
    
    Returns:
        dict: {success, filename, error, method}
    """
    input_path, output_dir = args
    filename = os.path.basename(input_path)
    basename = os.path.splitext(filename)[0]
    output_path = os.path.join(output_dir, basename + ".md")

    # 如果输出已存在,跳过
    if os.path.exists(output_path):
        return {
            "success": True,
            "filename": filename,
            "method": "skip_exists",
            "error": "",
        }

    ext = os.path.splitext(filename)[1].lower()
    system = platform.system()

    if ext == ".docx":
        success = convert_docx_with_pandoc(input_path, output_path)
        method = "pandoc"
    elif ext == ".doc":
        if system == "Darwin":
            success = convert_doc_macos(input_path, output_path)
            method = "textutil"
        else:
            success = convert_doc_linux(input_path, output_path)
            method = "libreoffice+pandoc"
    else:
        return {
            "success": False,
            "filename": filename,
            "method": "",
            "error": f"不支持的格式: {ext}",
        }

    return {
        "success": success,
        "filename": filename,
        "method": method,
        "error": "" if success else f"转换失败 ({method})",
    }


# ============================================================
# 批量转换
# ============================================================

def batch_convert(
    input_dir: str,
    output_dir: str,
    workers: int = 4,
    limit: int = 0,
) -> dict:
    """
    批量转换目录下所有 docx/doc 文件
    
    Args:
        input_dir: 输入目录
        output_dir: 输出目录
        workers: 并发进程数
        limit: 最多处理多少个文件 (0=全部)
    
    Returns:
        dict: 统计信息
    """
    os.makedirs(output_dir, exist_ok=True)

    # 收集所有文件
    files_to_convert = []
    for root, dirs, files in os.walk(input_dir):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in (".docx", ".doc", ".docm"):
                files_to_convert.append(os.path.join(root, f))

    files_to_convert.sort()

    if limit > 0:
        files_to_convert = files_to_convert[:limit]

    total = len(files_to_convert)
    print(f"待转换文件: {total} 个")
    print(f"输出目录: {output_dir}")
    print(f"并发进程: {workers}")
    print(f"系统: {platform.system()}")
    print()

    # 准备参数
    task_args = [(f, output_dir) for f in files_to_convert]

    stats = {
        "total": total,
        "success": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
        "methods": {},
    }

    start_time = time.time()

    # 多进程转换
    with Pool(workers) as pool:
        for i, result in enumerate(pool.imap_unordered(convert_single_file, task_args)):
            if result["success"]:
                if result["method"] == "skip_exists":
                    stats["skipped"] += 1
                else:
                    stats["success"] += 1
                method = result["method"]
                stats["methods"][method] = stats["methods"].get(method, 0) + 1
            else:
                stats["failed"] += 1
                stats["errors"].append({
                    "file": result["filename"],
                    "error": result["error"],
                })

            # 进度显示
            done = i + 1
            if done % 500 == 0 or done == total:
                elapsed = time.time() - start_time
                rate = done / elapsed if elapsed > 0 else 0
                eta = (total - done) / rate if rate > 0 else 0
                print(f"  进度: {done}/{total} "
                      f"({done/total*100:.1f}%) "
                      f"成功:{stats['success']} "
                      f"跳过:{stats['skipped']} "
                      f"失败:{stats['failed']} "
                      f"速度:{rate:.0f}文件/秒 "
                      f"剩余:{eta:.0f}秒")

    elapsed = time.time() - start_time
    stats["elapsed"] = elapsed

    print()
    print("=" * 60)
    print(f"转换完成!")
    print(f"  总文件: {stats['total']}")
    print(f"  成功: {stats['success']}")
    print(f"  跳过(已存在): {stats['skipped']}")
    print(f"  失败: {stats['failed']}")
    print(f"  耗时: {elapsed:.1f}秒")
    print(f"  平均速度: {stats['total']/elapsed:.0f} 文件/秒")
    print()
    print("转换方法统计:")
    for method, count in sorted(stats["methods"].items()):
        print(f"  {method}: {count} 文件")

    if stats["errors"]:
        print()
        print(f"失败文件 ({len(stats['errors'])} 个):")
        for e in stats["errors"][:20]:
            print(f"  - {e['file']}: {e['error']}")
        if len(stats["errors"]) > 20:
            print(f"  ... 还有 {len(stats['errors'])-20} 个")

    return stats


# ============================================================
# 命令行入口
# ============================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="批量转换 docx/doc 为 markdown")
    parser.add_argument(
        "--input-dir",
        default=None,
        help="输入目录 (默认: 法律法规库/地方法规/解压文件)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="输出目录 (默认: 法律法规库/地方法规_markdown)",
    )
    parser.add_argument(
        "--workers", type=int, default=4,
        help="并发进程数 (默认: 4)",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="最多处理多少个文件 (0=全部, 默认: 0)",
    )
    args = parser.parse_args()

    # 默认路径: 从当前工作目录查找法律法规库
    cwd = os.getcwd()
    LAW_BASE = os.path.join(cwd, "法律法规库")
    if not os.path.exists(LAW_BASE):
        # 尝试项目根目录 (脚本向上4层)
        script_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.dirname(script_dir))
        )))
        LAW_BASE = os.path.join(project_root, "法律法规库")

    input_dir = args.input_dir or os.path.join(LAW_BASE, "地方法规", "解压文件")
    output_dir = args.output_dir or os.path.join(LAW_BASE, "地方法规_markdown")

    if not os.path.exists(input_dir):
        print(f"错误: 输入目录不存在: {input_dir}")
        sys.exit(1)

    stats = batch_convert(
        input_dir=input_dir,
        output_dir=output_dir,
        workers=args.workers,
        limit=args.limit,
    )

    # 保存错误日志
    if stats["errors"]:
        import json
        log_path = os.path.join(output_dir, "conversion_errors.json")
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2, default=str)
        print(f"\n错误日志已保存: {log_path}")
