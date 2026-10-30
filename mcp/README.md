# 接入 agent（MCP）

让 WorkBuddy、Trae、ZCode、Claude Code、Codex、Cursor 这些 agent 直接查法条，
不必你再复制粘贴。

接入之后，你对 agent 说「帮我核对一下这段代理意见里引的法条」，
它会真的去库里逐条比对原文和时效性，而不是凭记忆编。

---

## 一、这套东西给你什么

四个工具，全部只读：

| 工具 | 什么时候用 |
|---|---|
| `get_article` | 已经知道出处，要拿逐字原文。如「劳动合同法第八十二条」 |
| `get_law` | 不确定条号，先看目录；或要通读整部 |
| `search_articles` | 只记得关键词，找候选条文 |
| `verify_citation` | **手里已有一段引用，要核它真假、是不是现行版本** |

`verify_citation` 是最该常用的一个。agent 编法条的时候，条号往往"看起来对"，
只有拿库去比才知道库里到底有没有这一条、这一条现在还有没有效。

> **一个必须说清的限制**：`search_articles` 是**字面匹配**，不是语义检索。
> 它不认识同义词——「欠薪」和「拖欠劳动报酬」在它眼里是两个词。
> 所以正确的用法是让 agent 先把你的口语拆成法律术语再查，
> 而不是指望它像搜索引擎那样"懂你的意思"。这也是这套东西主打
> 「精确查询」而不是「智能检索」的原因。

---

## 二、三步装好

### 第 1 步：确认库文件在

MCP 读的是 `data/law_data.db`。这个文件不进 git（2 GB 级，GitHub 单文件上限 100 MB），
要从仓库的 **Releases** 页面下载，解压后放进 `data/` 目录：

```
china-law-search/
└── data/
    └── law_data.db      ← 放这里
```

### 第 2 步：看本机装了哪些客户端

```bash
python3 mcp/install.py --list
```

会列出每家客户端、它的配置文件在哪、文件是已存在还是待创建。
没装的会标「未安装」，脚本不会去乱建目录。

### 第 3 步：先预览，再写入

```bash
python3 mcp/install.py --client workbuddy          # 只预览，不动文件
python3 mcp/install.py --client workbuddy --yes    # 确认后写入
python3 mcp/install.py --all --yes                 # 一次写完所有已装的
```

**默认不会写任何东西**，不加 `--yes` 只打印将要发生什么。改的是别的软件的配置文件，
猜错一次的代价是那个软件起不来。

写入前会把你原来的文件备份成 `mcp.json.bak-20261030-120809` 这种名字，出问题直接改回来。

要撤销：

```bash
python3 mcp/install.py --all --yes --remove
```

只想手动改？打印一份路径已经填好的片段，直接粘：

```bash
python3 mcp/install.py --print workbuddy
```

---

## 三、各家客户端的具体情况

配置路径和格式各家都不一样，下面这些是本项目**实际核对过**的结果。
标「未实测」的几家，本机没装，只能按官方文档写，**没有跑通过**。

### WorkBuddy（腾讯）

- 配置文件：`~/.workbuddy/mcp.json`（注意是 `mcp.json`，不是 `.mcp.json`）
- 格式：`mcpServers`，条目带 `"type": "stdio"`
- **写文件不等于生效**：写完必须打开 WorkBuddy →
  左侧「专家·技能·连接器」→ 连接器 → 右上角「自定义连接器」→ MCP 服务管理 →
  找到 `yilv-law` → 点**「信任」**。不点这一下，工具不会出现。
- 已实测。

### Trae（字节）

- 用户级配置文件路径**各家说法不一致**，官方论坛、第三方文档、TRAE Work 版
  各指一处。脚本的做法是：把三个候选路径都列出来，**只写那个父目录已经存在的**。
- 候选：`~/Library/Application Support/Trae/User/mcp.json`、
  `.../Trae/User/settings/mcp.json`、`.../TRAE SOLO CN/User/mcp.json`
- 格式：`mcpServers`，条目**不带** `type`
- 写完要**完全退出并重启** Trae。
- **未实测**。如果重启后没出现，改用 Trae 设置 → MCP → 手动配置，
  把 `python3 mcp/install.py --print trae` 的输出粘进输入框。

### ZCode（智谱）

- 配置文件：`~/.zcode/cli/config.json`
- 格式：**`mcp.servers`**，不是 `mcpServers`。这是 ZCode 自己的命名，
  与其它几家不一样，粘错了不会报错、只是不生效。
- 改完要**新开一个会话**（老会话读不到新配置）。
- 已实测。
- 另外提醒一个坑：ZCode 也认 `~/.agents/mcp.json`，但**只要 `.zcode` 配置里有任何服务，
  同作用域的 `.agents/mcp.json` 就会整份被跳过**（不是同名覆盖，是全部失效）。
  所以脚本统一写 `.zcode`，不碰 `.agents`。

### Claude Code

- 配置文件：`~/.claude.json`（user scope，所有项目可用）
- 格式：`mcpServers`，条目带 `"type": "stdio"`
- 官方也支持命令行，等价写法：

  ```bash
  claude mcp add yilv-law -- <python 绝对路径> <server.py 绝对路径>
  ```

- 已实测。

### Codex CLI（OpenAI）

- 配置文件：`~/.codex/config.toml`
- 格式：**TOML**，不是 JSON。段落名 `[mcp_servers.yilv-law]`，
  环境变量放在子表 `[mcp_servers.yilv-law.env]`
- 追加后重开终端。
- 已实测（含反复装卸不残留）。

### Cursor

- 配置文件：`~/.cursor/mcp.json`
- 格式：`mcpServers`，条目不带 `type`
- 写完在 Cursor → Settings → MCP 确认开关是开的，然后重启。
- **未实测**（本机没装 Cursor）。

### Cline（VS Code 扩展）

- 配置文件在 VS Code 的 globalStorage 里，路径较长：

  ```
  macOS:   ~/Library/Application Support/Code/User/globalStorage/
           saoudrizwan.claude-dev/settings/cline_mcp_settings.json
  Windows: %APPDATA%\Code\User\globalStorage\
           saoudrizwan.claude-dev/settings\cline_mcp_settings.json
  ```

- 没装这个扩展时目录不存在，脚本自动跳过。
- **未实测**。

### Windsurf

- 配置文件：`~/.codeium/windsurf/mcp_config.json`
- 写完在 Cascade → MCP 面板点 Refresh。
- **未实测**。

---

## 四、为什么脚本里写的都是绝对路径

因为 agent 拉起 MCP 子进程时的**工作目录不是你能控制的**，`PATH` 也常常
拿不到你在终端里配的那一份（GUI 程序启动时环境变量往往不完整）。

所以脚本写入的是：

```json
{
  "command": "/Users/你/china-law-search/venv/bin/python",
  "args": ["/Users/你/china-law-search/mcp/server.py"],
  "env": { "LAW_DB_PATH": "/Users/你/china-law-search/data/law_data.db" }
}
```

三个全是绝对路径。代价是你移动了文件夹就得重跑一次 `install.py`；
好处是「MCP 连上了但每个工具都报错」这类问题基本不会再出现。

写 `command` 时用的是**当前运行 install.py 的那个解释器路径，且故意不解析软链**。
venv 里的 `python` 通常是指向系统 python 的符号链接，一解析就把 venv 丢了，
写进去的路径看起来对、实际用的是另一个环境。

---

## 五、自己先测一下

不用等装进客户端，直接在终端跑自检：

```bash
python3 mcp/server.py --selftest
```

它会依次检查：库能不能打开、协议握手、四个工具是否注册、随机抽一条法条
做取原文和核验。全过就是好的。

装进客户端之后如果工具不出现，按这个顺序查：

1. **库文件在不在**：`data/law_data.db` 存在且**不是 0 字节**。
   空文件会让所有查询报 `no such table`，但看起来一切正常。
2. **手动跑一次 server.py**：`python3 mcp/server.py` 正常会往 stderr 打一行
   `started, db=...`，然后安静等输入。报错就是路径或权限问题。
3. **看客户端有没有「信任」或「启用」这一步**：WorkBuddy 需要点信任，
   好几家需要重启，这些脚本做不到。
4. **路径里有没有空格和中文**：本项目自己的路径就带中文括号，实测可用；
   但如果你的系统上有问题，优先怀疑这里。

---

## 六、数据口径（请务必知道）

- 库里是**构建时的快照**，不是实时同步。每个工具返回都带 `data_snapshot`
  字段，写明这一份库的 `corpus_version`。
- 条文本身来自公开的法律文本，但**任何正式引用都应以国家法律法规数据库
  （flk.npc.gov.cn）公布的文本为准**。
- 本项目不代你做时效性自动判定，也不承诺"绝对最新"。
  它给的是：**可查、可核、可追溯的原文**。判断仍在你手里。
- 全部工具只读。这里没有任何写库的路径，agent 也改不了你的数据。
