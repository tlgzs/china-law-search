# Yilvai Law Search API — 中国法律法规检索 API

开箱即用的中国法律法规检索服务：**ChromaDB 向量语义检索 + SQLite FTS 精确查找** 双库架构，附带从国家法律法规数据库原始数据到可检索库的完整数据流水线。

- 收录现行有效法律法规 **1.7 万余部、法条 75 万余条**
- 数据来源：国家法律法规数据库（flk.npc.gov.cn）
- 五个 API 端点：语义检索 / 精确条号 / 批量引文校验 / 库统计 / 健康检查
- 向量库不可用时自动降级 SQLite FTS，零配置即可跑通最小模式

## 架构

```
原始法规 Markdown（flk.npc.gov.cn 下载）
        │  ingest/convert_docx_batch.py   （可选：doc/docx 批量转 Markdown）
        ▼
结构化解析  ingest/parse_markdown.py      （逐条解析：条号/章节/公布施行日期）
        ▼
历史版本去重  ingest/dedup_old_versions.py（同一法律多版本去重）
        ▼
┌──────────────────────────┬────────────────────────────┐
│  ChromaDB 向量库          │  SQLite law_data.db        │
│  ingest_to_vectordb.py   │  ingest_to_sqlite.py       │
│  (BAAI/bge-large-zh-v1.5)│  (FTS5 trigram 全文索引)    │
└────────────┬─────────────┴──────────┬─────────────────┘
             └──────────┬─────────────┘
                        ▼
          server/law_search_service.py  (FastAPI, 端口 8001)
```

## 快速开始（最小模式：仅 SQLite）

```bash
# 1. 安装依赖
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. 放置数据文件（见下方「数据获取」），或自己跑流水线生成
#    ./data/law_data.db

# 3. 启动服务
uvicorn server.law_search_service:app --host 0.0.0.0 --port 8001

# 4. 验证
curl http://127.0.0.1:8001/health
```

最小模式下服务照常可用：`/api/law/search` 自动降级为 SQLite FTS 全文检索。

## 启用向量语义检索（可选）

```bash
# 用流水线构建向量库（需要下载 BAAI/bge-large-zh-v1.5 模型，约 1.3G）
python ingest/parse_markdown.py --help        # 解析原始 Markdown
python ingest/ingest_to_vectordb.py --markdown-dir <法规md目录> --batch-size 200
python ingest/ingest_to_sqlite.py             # 从 Chroma 元数据生成 law_data.db
```

## 数据获取

三种方式任选：

1. **下载现成数据包**（推荐）：从本项目 GitHub Releases 下载 `law_data.db`，解压到 `./data/`。
2. **自己采集**：到国家法律法规数据库 flk.npc.gov.cn 下载法规文件（官网提供批量下载能力），放入 `ingest/法律法规库/` 后运行流水线。
3. **Docker 一键起**：`docker compose up -d`（把数据放到 `./data/` 即可，容器会挂载该目录）。

## API 一览

所有业务端点需要 Bearer Token（`Authorization: Bearer <token>`，配置见 `data/api_keys.json`）。`/health` 免认证。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/law/search?q=...&k=10&type=法律` | 语义检索，返回 k 条法条（含相似度 score） |
| GET | `/api/law/article?law_name=民法典&article_num=680` | 精确条号查找（法名支持模糊匹配） |
| POST | `/api/law/verify` | 批量引文校验：存在性 / 时效 / 废止信息 / 原则性条款 / 多版本歧义 |
| GET | `/api/law/stats` | 库统计（总量、分类计数、向量库状态） |
| GET | `/health` | 双库健康状态，免认证 |

### 校验接口示例

```bash
curl -X POST http://127.0.0.1:8001/api/law/verify \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"citations": ["《劳动合同法》第八十二条", {"law_name": "民法典", "article_number": "第六百八十条"}]}'
```

返回每条引用的 `verified`、现行 `status`、`effective_date`、`repeal_date/reason`、`replaced_by`、`is_principle_clause`、`has_multiple_versions` 等，适合给 AI 生成的法律文书做引文核验。

## API Key 配置

`data/api_keys.json`（不入库，已在 .gitignore）：

```json
{
  "_comment": "tier=private 不限流；tier=public 按 IP 限流",
  "my-local-dev": "any-random-token-string",
  "public-app": {
    "token": "another-token",
    "tier": "public",
    "rate_limit_per_min": 10,
    "rate_limit_per_day": 1000
  }
}
```

本地自用想免认证：设环境变量 `LAW_API_ENFORCE=0`（未带 token 的请求以 `unknown` 渠道放行并记录）。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LAW_DB_PATH` | `<项目>/data/law_data.db` | SQLite 路径 |
| `LAW_CHROMA_PATH` | `<项目>/data/legal_vector_db` | ChromaDB 路径 |
| `LAW_COLLECTION_NAME` | `chinese_laws` | Chroma 集合名 |
| `LAW_EMBEDDING_MODEL` | `BAAI/bge-large-zh-v1.5` | Embedding 模型 |
| `LAW_API_KEYS_FILE` | `<项目>/data/api_keys.json` | API Key 配置 |
| `LAW_CALL_LOG_PATH` | `<项目>/data/call_log.jsonl` | 调用日志（JSONL） |
| `LAW_API_ENFORCE` | `1` | 强制认证开关 |
| `LAW_HOST` / `LAW_PORT` | `0.0.0.0` / `8001` | 监听地址端口 |

## 适用与不适用

**适合**：给 AI 应用/Agent/法律工具提供法条检索与引文核验后端；本地或私有化部署的法律研究工具。

**不适合**：作为正式法律意见的唯一依据——法条内容请以官方发布为准；本项目不提供司法解释推荐、类案检索或任何结果预测。

## 数据说明

- 法规文本来自国家法律法规数据库（flk.npc.gov.cn）。法律法规属于官方发布的文件，依法不受著作权保护，可自由分发使用。
- 数据按「现行有效」口径收录（历史版本已去重）；法规会持续修订，使用前建议通过 `/api/law/verify` 复核时效状态。

## License

MIT
