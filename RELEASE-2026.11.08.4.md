# 发布操作单 · 数据 2026.11.08.4 / app 2.4.0

> 本文件是发布操作清单，不是 Release 正文。下面的「Release 正文」一段可直接整段粘贴到 GitHub Release。
> 生成时间：2026-11-08　数据源：`china-law-search-api/dist/`（sha256 为本次实测）

---

## 一、要上传的文件（共 3 个，全部在 `dist/`）

| # | 文件名 | 大小（字节） | 大小（MiB） | sha256 |
|---|---|---|---|---|
| 1 | `manifest.json` | 722 | 0.0007 | `d3ca7b997c40f747c4f8276fc9a833373253d56cf1e2ec5f8274229a66ce94c9` |
| 2 | `law_data-2026.11.08.4.db.zst` | 688,001,661 | 656.1 | `b7a8d8ea4cedf52b5b32b69450251109b301e2e0cafe0c52b965abb715abf451` |
| 3 | `law_data-2026.11.08.4.db.gz` | 873,471,780 | 833.0 | `e4d64cf036f4528d90352ffdaf90073fd0ab82f188c2fb31db198efd37b9a1bd` |

绝对路径前缀：`/Users/fumin/Documents/G个人学习/AI学习/lawyer_model（远程开发）/china-law-search-api/dist/`

上传要求：

- **两个压缩格式都要传**，不是冗余。用户机器上可能既没有 `zstandard` 库也没有 `zstd` 命令，只能退回 `.gz`，只发 `.zst` 会让这类用户装不上。
- 文件名**一个字都不能改**，必须与 `manifest.json` 里 `artifacts[].name` 逐字一致，否则客户端挑不到资产。
- 不要勾 `Pre-release`（`fetch_data.py` 走 `/releases/latest`，GitHub 会跳过预发布）。
- 不要传 `.db` 原库；不要传 11.03.2 / 11.03.3 的旧资产（已发布）。

## 二、tag 与勾选项

| tag | 用途 | 必须做的操作 |
|---|---|---|
| `data-2026.11.08.4` | 数据发布 | 发布时勾 **Set as the latest release**；Release label 保持 `None` |
| `app-v2.4.0` | 程序版本比对（`check_update.py` 读 `TAG_PREFIX = "app-v"`） | **空 tag，不挂任何资产**（程序靠 `git pull` 更新，不发二进制） |

> 远端当前一个 `app-v*` tag 都没有，所以 `check_update.py` 会提示「远端没有 app-v* 的 tag，程序版本无法比对」。建空 tag `app-v2.4.0` 即可消除该提示。

## 三、本版不含增量包（重要）

**2026.11.08.4 没有增量包。** `dist/` 下不存在 `delta-2026.11.08.4.json.gz/.zst`，`state/change-ledger.json` 仍是 529 部法（与 11.03.3 同期）。

后果：**老用户会下载全量包**（zst 656.1 MiB / gz 833.0 MiB），而不是几 MB 的增量包。

如果之后补了增量包，本文件必须同步更新：上传文件变为 5 个（增加 `delta-2026.11.08.4.json.gz` 与 `.zst`），「一、要上传的文件」表格和「Release 正文 → 升级」一段都要改。

---

# 以下为可整段粘贴的 Release 正文

## 数据 2026.11.08.4（app 2.4.0）

法规 16,835 部，全库条文 1,044,621 条，版本 24,099 个。manifest 的 `article_count=702,825`（非废止口径，含已公布待生效版本）。

### 本版变更（实测）

- 三批 apply 全部入库：1,042,783 → 1,044,621，**净增 1,838 条**
- **原位更新 2,729 条**，新增 1,838 条，删除 0 条
- **空正文条文 0 条**；版本 `article_count` 与实存不一致 0 个
- `pragma quick_check` = ok；FTS 索引行数 = 1,044,621
- **G7 条文切分阻断 0 项**（告警 1 项：11 条 `source_long` 与 43 条 `source_gap` 均已登记，未补号、未造号）
- 本轮不含 batch4 的 42 个缺料版本（源文件本身缺号或重号），未写入

### 本版不含增量包

本版没有 `delta` 资产。所有用户（包括已装 2026.11.03.x 的用户）都会下载全量包：

- `law_data-2026.11.08.4.db.zst` — 656.1 MiB（优先）
- `law_data-2026.11.08.4.db.gz` — 833.0 MiB（无 zstd 环境时使用）

### 校验

下载后对照本 Release 的 `manifest.json`：

1. 全量包的 `sha256` 必须等于 `manifest.json` → `artifacts[].sha256`
2. 解压后整库 `sha256` 必须等于 `manifest.json` → `db_sha256`，即
   `7ea020311755fe88dcdb757cc7ec52e34286e07d42800e01e213383714e2ce64`

```bash
shasum -a 256 law_data-2026.11.08.4.db.zst   # Linux: sha256sum
python3 tools/fetch_data.py --check          # 安装后核对版本
```

### 升级

```bash
python3 tools/check_update.py --update
# 或 ./update.sh
# macOS 也可双击「更新数据.command」
```

程序侧请先 `git pull` 再装数据。本版无增量包，会走全量下载。

### 回滚

```bash
# 1) 先给当前 2.4.0 库留一份（APFS 克隆，几乎不占空间）
cp -c data/law_data.db data/law_data.db.rollback-$(date +%Y%m%d)

# 2) 停检索服务
pkill -f "server.law_search_service"

# 3) 覆盖回 pre-240（2026.11.07.2，1,042,783 条）
cp -c data/law_data.db.pre-240-20261108 data/law_data.db

# 4) 校验后重启
shasum -a 256 data/law_data.db    # 应为 27ee1ecc…ac3853
```

回滚后 `/health` 的 `count` 应回到 1,042,783。
