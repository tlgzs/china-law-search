# 壹律法条 · China Law Search

把国家法律法规数据库（flk.npc.gov.cn）的条文落到本地，用一个 SQLite 文件 + 一个 FastAPI 服务，
给律师和法务提供**可离线、可自建、可增量更新**的法条检索与引文核验能力。

四种用法，按需要选：

- **接进 agent（MCP）**：WorkBuddy、Trae、ZCode、Claude Code 等直接查法条，
  用条文支撑结论前自动核验真假与时效。
- **本地查询界面**：双击启动脚本，浏览器打开就能搜法条、按条号定位、批量核验引文。
- **HTTP API**：接进自己的办案系统、文书工具、AI 工作流。
- **自建数据库**：用内置流水线把官方 Markdown 解析入库，或按增量包持续更新。

## 能力

| 能力 | 说明 |
|---|---|
| 法条检索 | 关键词检索，FTS5 全文索引 + bm25 相关性排序；装向量库后可切换语义检索 |
| 条文定位 | 按「法律名称 + 条号」直接取条文，支持「第八十二条之一」这类增补条 |
| 引文核验 | 批量提交引文，逐条核对是否存在、是否现行有效、是否已被修订、是否原则性条款 |
| agent 接入 | 内置 MCP 服务器（stdio，零第三方依赖），四个只读工具，见 [`mcp/README.md`](mcp/README.md) |
| 版本保留 | 被修订的旧条文不删除，标记为已失效；默认不返回，需要时显式开启 |
| 增量更新 | 下载新版 Word/Markdown → 生成变更报告 → 人工确认 → 写库，全程可审计 |
| 离线运行 | 检索不依赖任何外部接口，内网、无网环境可用 |

## 快速开始

### 方式一：双击启动（推荐给不写代码的用户）

1. 安装 Python 3.10 或更高版本（Windows 安装时勾选 `Add Python to PATH`）。
2. 从 Releases 下载数据包，解压后把 `law_data.db` 放进 `data/` 目录。
3. macOS 双击 `启动.command`；Windows 双击 `启动.bat`。

首次运行会自动创建虚拟环境并安装依赖（约 1 分钟），随后自动打开
`http://127.0.0.1:8001`。数据包缺失时服务仍会启动，只是查不到内容。

### 方式二：命令行

```bash
pip install fastapi "uvicorn[standard]"
python -m uvicorn server.law_search_service:app --host 127.0.0.1 --port 8001
```

或直接 `./start.sh`。

### 方式三：Docker

```bash
docker compose up -d
```

数据目录 `./data` 挂载进容器，把 `law_data.db` 放进去即可。

## 数据从哪来

法规文本来自**国家法律法规数据库**（`flk.npc.gov.cn`）公开下载的 Markdown 文件，
属官方文件，可自由使用与再分发。

数据不放进代码仓库（体积原因），以 GitHub Release 附件发布：

- `law_data-<版本>.db.zst`：全量库，新用户首次下载
- `delta-<版本>.json.zst`：增量变更集，老用户只下这个
- `manifest.json`：版本号、校验和、schema 版本、变更摘要

数据包体积较大（数百 MB 级），下载后解压到 `data/` 目录。
老用户升级不必重新下载全量：

```bash
python tools/apply_delta.py --delta delta-2026.10.30.json --db data/law_data.db
```

## 本地查询界面

服务启动后打开 `http://127.0.0.1:8001`，三个页签：

- **法条检索**：输入关键词，勾选「含已被修订的旧版本条文」可一并看到历史条文，
  失效版本带红标并排在现行条文之后。
- **条文定位**：填法律名称与条号（`82` 或 `第八十二条` 都认），增补条在第三栏填 `之一`。
- **引文核验**：一行一条引文，例如 `《劳动合同法》第八十二条`，一次核完。

界面完全离线，不加载任何外部资源。

## 接入 agent（MCP）

不想复制粘贴，让 agent 自己查。内置一个 MCP 服务器，四个只读工具：

| 工具 | 作用 |
|---|---|
| `get_article` | 法名 + 条号 → 逐字原文（支持「之一」增补条） |
| `get_law` | 法名 → 章节目录 + 版本沿革 + 条文 |
| `search_articles` | 多关键词 → 候选条文（字面匹配，不做语义理解） |
| `verify_citation` | 一段引文 → 是否逐字命中、命中的是否现行版本 |

```bash
python3 mcp/install.py --list              # 看本机装了哪些客户端
python3 mcp/install.py --all --yes         # 写入（默认只预览，加 --yes 才落盘）
python3 mcp/server.py --selftest           # 装完自检
```

安装器覆盖 WorkBuddy、Trae、ZCode、Claude Code、Codex、Cursor、Cline、Windsurf，
自动探测配置位置、备份原文件、合并写入（不动别人已有的 MCP 条目）。

**两个必须知道的前提**：

- 写文件不等于生效。WorkBuddy 要在连接器页点「信任」，Trae / Cursor 要重启，
  这些脚本做不到，逐步说明见 [`mcp/README.md`](mcp/README.md)。
- MCP 直连 SQLite，**不需要先启动服务**——agent 自己拉起子进程，用完即退。
  这也是它只要 Python 标准库、不装 `mcp` SDK 的原因。

## API

服务默认**免认证**（本地自用）。需要对外开放时设 `LAW_API_ENFORCE=1`，
并在 `data/api_keys.json` 配置 token：

```json
{
  "my-app": {
    "token": "换成你的随机串",
    "tier": "private",
    "rate_limit_per_min": 0,
    "rate_limit_per_day": 0
  }
}
```

`tier` 为 `public` 时按 IP 限流，超额返回 `429` 与 `Retry-After`。
注意：开了强制认证却读不到任何 token 时，服务会自动降级为免认证，避免装完打不开。

### `GET /health`

免认证。返回服务状态、法条总数、现行有效条数、向量库是否启用。

### `GET /api/law/search`

| 参数 | 必填 | 说明 |
|---|---|---|
| `q` | 是 | 查询词 |
| `k` | 否 | 返回条数，默认 10，上限 50 |
| `type` | 否 | 法规类型过滤，如 `law`、`administrative_regulation` |
| `include_history` | 否 | `true` 时包含已失效的旧版本条文 |

```bash
curl -G http://127.0.0.1:8001/api/law/search \
  --data-urlencode "q=未签书面劳动合同" \
  --data-urlencode "k=5"
```

检索走三级策略，逐级保底：

1. FTS5 trigram 全文索引，bm25 排序；
2. 字面包含匹配；
3. 把查询拆成两字片段，命中多数片段即召回（中文不靠分词也能兜住改写）。

返回的 `engine` 字段标明实际走的路径，`items[].score` 为 0~1 的相关度。

### `GET /api/law/article`

| 参数 | 必填 | 说明 |
|---|---|---|
| `law_name` | 是 | 法律名称，支持简称模糊匹配 |
| `article_num` | 是 | 条号数字 |
| `article_suffix` | 否 | 增补条后缀，如 `之一` |
| `include_history` | 否 | 是否返回已失效版本 |

```bash
curl -G http://127.0.0.1:8001/api/law/article \
  --data-urlencode "law_name=劳动合同法" \
  --data-urlencode "article_num=82"
```

### `POST /api/law/verify`

```bash
curl -X POST http://127.0.0.1:8001/api/law/verify \
  -H "Content-Type: application/json" \
  -d '{"citations": ["《劳动合同法》第八十二条", "刑法第一百三十三条之一"]}'
```

每条结果包含 `verified`、`status`、`effective_date`、`repeal_date`、`replaced_by`、
`is_principle_clause`、`has_multiple_versions`。`has_multiple_versions` 只在**同一部法规**
存在多个版本时为真，不会因为法规名相近而误报。

### `GET /api/law/stats`

返回按法规类型分组的条数统计、现行有效条数、向量库状态。

## 自己建库

`ingest/` 是一条完整流水线：

```bash
# 1) 把官方下载的 doc/docx 转成 Markdown（需要系统装 pandoc 与 libreoffice）
python ingest/convert_docx_batch.py --input-dir ./raw --output-dir ./md

# 2) 解析条号与章节，写入 SQLite（唯一真源），同时建 FTS 索引
#    默认每部法规只取最新版本；要连历史版本一起入库加 --keep-history
python ingest/build_sqlite_from_markdown.py --markdown-dir ./md --output-path ./data/law_data.db

# 3) 可选：构建向量索引，启用语义检索
python ingest/ingest_to_vectordb.py --markdown-dir ./md --db-path ./data/legal_vector_db
```

**SQLite 是唯一真源，向量库只是可重建的索引。** 向量库损坏不影响数据，
删掉重跑第 3 步即可。不装 `chromadb` 与 `sentence-transformers` 时，
服务自动降级为全文检索，功能不受影响。

## 增量更新

法规会修订，库要跟着走。把下载到的新版文件丢进 `inbox/`：

```bash
# 第一步：扫描 → 转换 → 解析 → 比对 → 出报告（只出报告，不写库）
python tools/update.py --inbox inbox/

# 打开 reports/changes-<时间>.html 逐条看：新增条、修改条（左右对照）、删除条、元数据变更

# 第二步：确认无误后写库
python tools/update.py --apply reports/changes-<时间>.plan.json --approve
```

设计上刻意留了一道人工闸门：**没有 `--approve` 绝不写库**。法条改动直接影响律师引用，
不能自动生效。

写库是单事务完成的：新版本插入，旧版本标为已失效（记录失效日期与被替代关系），
同时写入 `ingest_batches` 审计记录。同一个文件重复投放不会重复入库（按文件 sha256 去重）。

流水线的完整设计（比对口径、版本模型、校验项）见 `docs/UPDATE-PIPELINE.md`。

## 发版

```bash
# 打包全量库 + 增量包 + manifest
python tools/make_release.py --db data/law_data.db --delta delta-2026.10.30.json --out-dir dist/

# 上传为 Release（需已安装 gh 并登录）
gh release create data-2026.10.30 dist/*.zst dist/manifest.json --title "数据 2026.10.30"
```

## 目录结构

```
server/
  law_query_core.py            法条查询内核（纯 sqlite3，HTTP 与 MCP 共用同一套规则）
  law_search_service.py        HTTP 服务（检索 / 定位 / 核验 / 统计）
mcp/
  server.py                    MCP 服务器（stdio，四个只读工具，零第三方依赖）
  install.py                   各客户端安装器（探测 / 备份 / 合并写入 / 移除）
  README.md                    各家客户端的配置位置与坑
web/index.html                 本地查询界面（单文件，零外部依赖）
ingest/                        建库流水线
  schema.py                    表结构与迁移（幂等，老库自动补列）
  build_sqlite_from_markdown.py  Markdown → SQLite + FTS
  parse_markdown.py            解析条号、章节、元数据
  dedup_old_versions.py        历史版本去重
  ingest_to_vectordb.py        构建向量索引（可选）
tools/
  update.py                    增量更新主程序（出报告 / 确认写库）
  diff_articles.py             条文级比对 + HTML 报告
  make_delta.py                生成增量包
  apply_delta.py               应用增量包
  make_release.py              打包发版资产
docs/UPDATE-PIPELINE.md        更新流水线的设计说明
```

**为什么单抽一个 `law_query_core.py`**：条号「之一」与法规名归一这两条规则，
一旦网页端和 MCP 端各写一份，迟早走岔——表现为「网页查得到、agent 查不到」。
而这种错在 agent 场景下不会有人复核，代价比网页高得多。所以只允许一份实现。

## 配置

全部通过环境变量，默认值见 `server/law_search_service.py` 顶部，示例见 `.env.example`。

| 变量 | 默认 | 说明 |
|---|---|---|
| `LAW_DATA_DIR` | `./data` | 数据目录，其余路径默认基于它 |
| `LAW_DB_PATH` | `$LAW_DATA_DIR/law_data.db` | SQLite 库路径 |
| `LAW_CHROMA_PATH` | `$LAW_DATA_DIR/legal_vector_db` | 向量库路径 |
| `LAW_API_ENFORCE` | `0` | `1` 时强制 Bearer Token |
| `LAW_API_KEYS_FILE` | `$LAW_DATA_DIR/api_keys.json` | 令牌表 |
| `LAW_CALL_LOG_PATH` | `$LAW_DATA_DIR/call_log.jsonl` | 调用日志 |
| `LAW_EMBEDDING_MODEL` | `BAAI/bge-large-zh-v1.5` | 向量模型 |
| `LAW_COLLECTION_NAME` | `chinese_laws` | 向量集合名 |

老版本数据包不需要手动迁移：服务启动时会自动补齐新增字段，可反复执行。

## 常见问题

**查不到想要的条文？** 先试更短的词。检索是关键词匹配，不是问答——
「公司不给上社保怎么办」这类整句提问，请换成「社会保险」等术语再搜。
装向量库后会好一些，但仍不应把它当作法律咨询。

**条文和官网对不上？** 以国家法律法规数据库发布的正式文本为准。
本项目只是把公开数据落到本地，不做任何改写。发现问题请提 issue 并注明法规名称与条号。

**能不能直接对公网开放？** 技术上可以，但请务必开 `LAW_API_ENFORCE=1` 并配置 token，
同时放在反向代理后面。默认的免认证模式只适合本机使用。

## 边界

- 本项目提供**条文文本与检索**，不提供法律意见，不替代律师判断。
- 引文核验只回答「库里有没有这条、是否现行有效」，不判断引文是否被正确适用。
- 条文时效以库内记录为准，数据版本日期见 `manifest.json`；涉及时效敏感的案件请自行复核。

## 许可

代码以 MIT 许可发布。法规文本源自国家法律法规数据库公开数据，按其公开使用规则使用。
