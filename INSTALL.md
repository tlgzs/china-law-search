# 安装 · 壹律法条

> **先看你属于哪一种，只看对应那一节就够：**
>
> - 不想碰命令行，想让 AI 替你装 → [第一节](#一不想碰命令行把这段话发给你的-ai)
> - 想自己动手，看命令 → [第三节](#三手动安装)
> - 装好了但打不开查询页面 → [第五节](#五打开本地查询界面)
> - 装了一半卡住了 → [第六节](#六装不上怎么办)

---

## 一、不想碰命令行：把这段话发给你的 AI

复制下面**整段**（从「我在自己电脑上」到最后一行），发给 WorkBuddy、Claude、ChatGPT、
Trae、Codex 任何一个**能读写本机文件**的 AI。它会自己检查环境、下载、安装、验证，
然后把结果报给你。

> 我在自己的电脑上安装开源项目「壹律法条」（`china-law-search`，
> 仓库地址 https://github.com/tlgzs/china-law-search ），
> 请严格按仓库里的 `INSTALL.md` 执行，具体要求：
>
> 1. **先检查环境**：本机有没有 `python3`（版本需 ≥ 3.9）和 `git`。
>    缺哪个就告诉我怎么装，**不要自己装系统级软件，也不要改系统 PATH**。
> 2. **选目录**：默认装在 `~/china-law-search`；如果我已有别的想法我会告诉你。
>    装之前告诉我你打算装到哪个路径。
> 3. **克隆仓库**：`git clone https://github.com/tlgzs/china-law-search.git`
> 4. **下载数据**：`cd china-law-search && python3 tools/fetch_data.py`
>    这一步会从 GitHub Release 下载约 676 MB 的数据库，会校验 sha256 后再就位。
>    装过 zstd 的话自动改下 514 MB 的 .zst 版本，内容一样。
>    如果它报「找不到仓库」，改用
>    `python3 tools/fetch_data.py --repo tlgzs/china-law-search` 重试。
> 5. **接入 AI 客户端**：`python3 mcp/install.py --all --yes`
>    它会把配置写进本机 AI 客户端的 MCP 配置，写入前会自动备份原文件。
> 6. **自检**：`python3 mcp/server.py --selftest`，把输出**原样**贴给我。
>    正常应该看到 5 行 `OK` 加最后一行「全部通过」，并且条文总数是 1,040,609。
> 7. **验收**：用 MCP 的 `get_article` 工具查「劳动合同法 第八十二条」，
>    把条文原文和返回值里的 `data_snapshot` 一起给我。查得到、条号对得上，才算装好。
> 8. **最后启动本地查询界面**：运行 `./start.sh`（Windows 用 `启动.bat`），
>    它会打开浏览器到 `http://127.0.0.1:8001`。确认页面顶部显示「服务正常」、
>    底部显示数据版本，然后告诉我浏览器已经打开了。这个服务要一直开着，我随时要查。
> 9. **告诉我以后怎么更新**：查有没有新版用 `python3 tools/check_update.py`，
>    查到就装用 `python3 tools/check_update.py --update`；macOS 也可以双击
>    `更新数据.command`。开着网页时它自己会提示，不用我去翻 GitHub。
>
> **安全边界，请遵守**：这套工具是**只读**的，不会修改、删除我任何文件；
> 除了第 4 步从 GitHub 下载数据包，全程不需要联网，也不会把我的文件上传到任何地方。
>
> 任何一步失败，请把**完整报错原文**贴出来，不要跳过、不要自己猜着改配置、
> 不要为了绕过报错而降低校验（比如跳过 sha256、跳过完整性检查）。

**为什么第 7 步要验收**：AI 装完最爱说「已经完成」。但「配置文件写了」不等于
「库下好了」也不等于「agent 真能查到条文」。第 7 步是唯一能证明它真的跑通的动作。
如果它查出来的条文跟你手上的法条文本对不上，直接说「不对，重来」。

---

## 二、AI 会做什么（你可以在旁边核对）

| 步 | 它执行什么 | 正常输出 | 耗时 |
|---|---|---|---|
| 1 | 检查 `python3` / `git` | 版本号 | 几秒 |
| 3 | 克隆仓库 | 50 个文件，约 810 KB | 10 秒内 |
| 4 | 下载数据库 | 进度条 → `sha256 校验通过` | 视网速，约 676 MB |
| 5 | 写 MCP 配置 | 列出探测到的客户端，逐个 `已写入` | 几秒 |
| 6 | 自检 | 5 行 `OK` + `全部通过` | 约 2 秒 |
| 7 | 查一个法条 | 条文原文 + 数据版本号 | 1 秒 |

第 4 步是关键，也是唯一可能慢的一步。**它不会盲目覆盖你的库**：先解压到临时文件、
校验 sha256 与数据库完整性，通过了才替换，旧库留一份 `.bak`。

---

## 三、手动安装

### 3.1 确认本机有 Python 3.9 以上

```bash
python3 --version
```

看到 `Python 3.9.x` 或更高就行。macOS 自带的 3.9 够用，不必另装。

Windows 上如果提示找不到 `python3`，换成 `python --version`；本文后面所有
`python3` 命令在 Windows 上相应换成 `python`。

没装的话：macOS 用 `xcode-select --install`（装命令行工具，自带 Python 3），
Windows 到 python.org 下载安装包，**安装时勾选 "Add Python to PATH"**。

### 3.2 装到哪、怎么装

```bash
git clone https://github.com/tlgzs/china-law-search.git
cd china-law-search
python3 tools/fetch_data.py
```

`fetch_data.py` 干四件事：读 Release 的 `manifest.json` → 比对本地库版本 →
下载（没库下全量，有库只下增量）→ 校验并替换。

常用参数：

```bash
python3 tools/fetch_data.py --check              # 只看版本，不下载
python3 tools/fetch_data.py --repo tlgzs/china-law-search   # 仓库名没自动识别出来时手动指定
python3 tools/fetch_data.py --full               # 强制重下全量
python3 tools/fetch_data.py --from-file ~/Downloads/law_data-2026.10.30.db.gz   # 离线安装
```

**公司网络下不动 GitHub**：用浏览器打开仓库的 Releases 页，手动下载
`law_data-<版本>.db.gz`，再用 `--from-file` 指过去。`.gz` 不需要装任何解压工具。

**磁盘要留够 4 GB**：库解压后 2.4 GB，下载的压缩包 676 MB，解压时还要一份临时文件。
**升级已有数据时要留 8 GB**——脚本会把旧库改名成 `law_data.db.bak` 留着，确认新版没问题后可以手动删掉它。

### 3.3 接入 AI 客户端

```bash
python3 mcp/install.py --list          # 先看本机装了哪些客户端
python3 mcp/install.py --all --yes     # 写入全部探测到的客户端
```

不加 `--yes` 只预览不落盘。脚本会自动探测配置位置、备份原文件（`.bak-时间戳`）、
合并写入，**不动你已有的其他 MCP 条目**。

也可以只装指定的一个：

```bash
python3 mcp/install.py --client workbuddy --yes
```

### 3.4 验证

```bash
python3 mcp/server.py --selftest
```

正常的输出长这样（路径和条号会不同）：

```
库路径：/Users/你的用户名/china-law-search/data/law_data.db
OK   initialize → protocol=2024-11-05
OK   tools/list → ['get_article', 'get_law', 'search_articles', 'verify_citation']
OK   stats → 条文 1040609 / 现行 700551 / 法规 16874
     快照 → corpus_version='2026.11.03.2' 条文截至=2026-10-17
OK   get_article → ... 第一条 (1018 字节)
OK   verify_citation → 1/2 命中（预期 1/2）

全部通过
```

第 3 行的三个数字对得上（条文 1,040,609 / 现行 700,551 / 法规 16,874），
说明库是完整的。数字差很多，就是库没下好，重跑 3.2。

「条文截至」是你手里这份数据的收录日期，回答时效性问题时要拿它说话。

### 3.5 以后怎么更新数据

先看一眼有没有新版（这条只查不装，几秒钟）：

```bash
python3 tools/check_update.py
```

它会分别报数据和程序：「法规库 2026.11.03.2 → 2026.11.30.1，可增量 3.2 MB」。
数据有新版本就装：

```bash
python3 tools/check_update.py --update
```

macOS 上也可以直接双击 **`更新数据.command`**，不用敲命令。

不习惯命令行的，直接开着网页就行：服务启动时会在后台查一次，有新版本会在
检索框上方亮一条琥珀色提示，点「立即更新」才装，点「×」本次不再打扰。

- 已经是最新：什么都不做，直接退出。
- 有更新：**只下载变更的那几 MB**，不用重下 676 MB 全量。本地停在哪一版都行，
  不用逐版追。
- 应用约 100 秒，大部分时间在重建全文索引，这期间别关窗口。
- 万一增量这条路不通，脚本会自己改下全量，不会把你停在报错上。
- 更新完**不用重启服务**，刷新网页即可 —— 库是同一个文件。
  只有提示「程序 2.3.0 → 2.4.0」时才需要 `git pull` 后重启。

---

## 四、还差最后一步：点「信任」

**配置文件写了不等于生效，这是最容易卡住的地方。** 各家客户端处理方式不同：

| 客户端 | 装完后要做什么 |
|---|---|
| **WorkBuddy** | 打开左下角 `+` → 连接器 → 右上角「自定义连接器」，找到 `yilv-law`，点**「信任」**。不点信任，工具列表里就没有它。 |
| **Trae** | 完全退出后重开（不是关窗口，是退出进程） |
| **Claude Code** | 重开终端会话，`/mcp` 里能看到 `yilv-law` 即成功 |
| **Codex CLI** | 重开终端会话；配置在 `~/.codex/config.toml` |
| **Cursor** | 重启 Cursor，在 MCP 面板里确认 `yilv-law` 是绿灯 |
| **Cline** | 重启 VS Code 窗口 |
| **Windsurf** | 重启 Windsurf |
| **ZCode** | 重启；注意它的配置在 `~/.zcode/.agents/mcp.json` 的 `mcp.servers` 下 |

工具列表是在**会话启动时**加载的。所以改完配置必须重开会话，中途改不会热更新。

---

## 五、打开本地查询界面

装好数据之后，**双击 `启动.command`（macOS）或 `启动.bat`（Windows）**，
脚本会自己起服务、等它就绪、再打开浏览器。命令行用户跑 `./start.sh` 效果一样。

打开后是这个页面：`http://127.0.0.1:8001`（端口被占会顺延到 8010 之间）。

三个页签，对应三种真实场景：

| 页签 | 用来干什么 | 典型操作 |
|---|---|---|
| **法条检索** | 只记得关键词，找候选条文 | 输入「二倍工资」回车；点结果展开全文与生效日期 |
| **条文定位** | 已经知道出处，要逐字原文 | 法名填「劳动合同法」，条号填 `82` 或「第八十二条」；增补条在第三栏填「之一」 |
| **引文核验** | 手里已有一段引用，核它真假和时效 | 一行一条粘贴，点核验，逐条看「通过 / 未通过」 |

几个用起来更顺的地方：

- **`⌘K` / `Ctrl+K`** 直接把光标跳到检索框。
- 首屏有八个常用查询，点一下直接搜，不用记关键词。
- 检索框下面能勾「含已被修订的旧版本条文」——核旧案卷里引的条文时要用。
- 页面完全离线，不加载任何外部字体、图标或统计脚本，断网也照常查。
- 底部会显示**数据版本**（如 `2026.10.30.1`）。写进意见书前看一眼，
  心里有数你引的是哪一版。

**这个页面不会自动关掉**。用完直接关浏览器标签就行，服务还在后台跑；
要停服务，回到终端按 `Ctrl+C`，或直接关掉那个命令行窗口。

**想让它开机自动起来**：macOS 上是「系统设置 → 通用 → 登录项」加 `启动.command`；
Windows 上是把 `启动.bat` 的快捷方式放进「启动」文件夹（`Win+R` 输入 `shell:startup`）。

**其他电脑能不能访问？** 默认只听 `127.0.0.1`，同一局域网的其他机器连不上——
这是有意的，避免你笔记本上的法规库被同事或访客无意翻到。
确实要给团队共用，需要显式改 `LAW_HOST` 并自行加上访问控制。


---

## 六、装不上怎么办

| 现象 | 原因 | 怎么办 |
|---|---|---|
| `command not found: python3` | Windows 或精简系统 | 换 `python`；仍不行就装 Python 并勾选 Add to PATH |
| `python3: command not found` 但在 Windows PowerShell | PowerShell 会去应用商店找 | 装完 Python 后重开 PowerShell |
| `fetch_data.py` 报 404 / 找不到 Release | 仓库名没识别出来，或 Release 还没发布 | 加 `--repo tlgzs/china-law-search`；或去 Releases 页确认有 `manifest.json` |
| 下载到一半断了 | 网络 | 直接重跑同一条命令，会重新下；旧库不会被破坏 |
| `sha256 不匹配` | 下载损坏或代理改写了内容 | 重跑；仍失败就手动下 `.gz` 包再 `--from-file` |
| `SSL: CERTIFICATE_VERIFY_FAILED` | 公司代理 / 内网拦截 | 用浏览器下包，再 `--from-file` |
| 自检条文数远小于 1,040,609 | 库没下好，或下的是旧版本 | 看 `--check` 输出，跑 `--full` 重下全量 |
| WorkBuddy 里看不到工具 | 没点「信任」，或没重开会话 | 见第四节 |
| agent 说「查不到这条法条」 | 可能真不在库里（部门规章不在） | 先用 `get_law` 查法规名是否存在，再判断 |
| `database is locked` | 有另一个进程正在写库 | 关掉正在跑的更新脚本再试 |
| 磁盘不足 | 库 2.4 GB + 压缩包 676 MB | 新装留 4 GB，升级留 8 GB |

判断「是装坏了还是本来就没有」的最快办法：

```bash
python3 mcp/server.py --selftest
```

自检过了，说明工具和库都是好的，查不到就是**这部法规不在库里**——
本库只收官方七类（宪法、法律、行政法规、监察法规、司法解释、法律解释、地方性法规），
**部门规章和地方政府规章不在其中**，这是数据源边界，不是装坏了。

---

## 七、卸载

```bash
python3 mcp/install.py --remove      # 从各客户端配置里删掉 yilv-law 条目
```

然后删掉整个仓库目录即可（数据就在 `china-law-search/data/` 里，一并删掉）。
`install.py --remove` 不会碰你自己的其他 MCP 配置。

---

**壹律从简** · [www.yilvai.com](https://www.yilvai.com) · 作者：网络小菜鸟 · 微信：`pmfumin`

按上面排查表还是卡住，把**原样报错**发我（微信或 [Issues](https://github.com/tlgzs/china-law-search/issues)），
别自己删库重来——数据包 676 MB，重下不值得。
