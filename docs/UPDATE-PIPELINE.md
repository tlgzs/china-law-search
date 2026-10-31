# 增量更新流水线设计

法规会修订、会废止、会出新版本。这个文档说明本项目的更新机制为什么这样设计，
以及每一步实际做了什么。

## 三条设计前提

**一、旧版本必须留。**

律师办的是过去发生的案子，引用的常常是**行为时**的条文。一条 2020 年的合同纠纷，
要引 2012 年版《劳动合同法》第八十二条，而不是现在这一版。所以「新版本入库、旧版本删除」
是不可接受的设计——旧条文必须能在库里查到，并明确标注失效日期和被谁取代。

**二、人工闸门不可省。**

法条一个字之差足以改变裁判结果。流水线可以自动比对、自动生成报告、自动写库，
但**不能自动决定采纳**。因此写库必须显式带 `--approve`，缺了这个参数一律拒绝。

**三、SQLite 是唯一真源。**

向量库只是索引，可以随时重建。早期版本是「先入 Chroma，再从 Chroma 元数据反建 SQLite」，
主从关系是反的——向量库损坏就会丢数据。现在反过来：先落 SQLite，再从 SQLite 生成向量。

## 数据模型

```
law_versions            一部法规的「一个版本」（公布日期 + 施行日期 + 来源文件）
  ├ id                  版本主键
  ├ law_name/norm_name  名称与归一化名称（去掉「中华人民共和国」前缀等）
  ├ publish_date        公布日期，判新旧的主键之一
  ├ article_count       该版本条文数
  ├ status              effective / superseded
  ├ superseded_by       被哪个版本取代
  └ batch_id            哪一批写入的

law_articles            条文，通过 version_id 归属到某个版本
  ├ version_id          → law_versions.id
  ├ article_number_num  条号数字（第八十二条 → 82）
  ├ article_suffix      增补后缀（之一 / 之二），与条号组成复合主键
  ├ content_hash        正文哈希，比对时用它判断是否改动
  └ status              effective / repealed（单条级失效）

ingest_batches          每次写库的审计记录（批次、时间、各项计数、依据的 plan 文件）
ingest_files            文件指纹表，sha256 → 版本，用于幂等
corpus_meta             键值表，存 schema_version 与 corpus_version
```

**条文主键是 `(version_id, article_number_num, article_suffix)`。** 加了后缀这一维，
「第一百三十三条」和「第一百三十三条之一」才不会被当成同一条而互相覆盖。

## 流水线

```
① 投放      把新版文件放进 inbox/
② 转换      doc/docx → Markdown（pandoc + libreoffice，可跳过）
③ 解析      逐份解析出法规名、公布/施行日期、条号、章节、正文
④ 比对      与库内同名法规的最新版本逐条对账（按条号+后缀为主键，正文比 content_hash）
⑤ 报告      生成 HTML（人看的）与 plan.json（机器用的），不写库
⑥ 确认      人逐条看过报告，加 --approve
⑦ 写库      单事务：插新版本 → 旧版本置 superseded → 旧条目标 repealed → 写审计
⑧ 索引      只重建本次涉及法规的向量，不碰全库
⑨ 自检      条数一致、条号不重、正文不为空；不一致直接报出来
```

### 第一步·投放

文件按 `sha256` 建指纹，重复投放同一份文件不会重复入库（幂等）。
所以「不确定某个文件导过没有」时，直接再放一次即可，不会产生重复版本。

### 第四步·比对

三类法规级结果：

| 动作 | 判定 |
|---|---|
| `new` | 库里没有这部法规 |
| `updated` | 同名法规已有版本，且内容或元数据有差异 |
| `skip` | 同名同版本且逐条一致（含元数据），不动 |

条文级三类差异：新增条、修改条（附左右对照与字符级差异）、删除条。
**删除条不物理删除**，写库时置为 `repealed`。

### 第五步·报告

`reports/changes-<时间>.html` 是给人读的，直接双击打开：

- 每部法规一张卡，标出动作（新增/修订/无变化）
- 修订的条文左右并排，改动的字以颜色标出
- 顶部汇总：新增几部、修订几部、新增条、修改条、删除条

同目录的 `.plan.json` 是写库依据，不手改。

### 第七步·写库

整个过程在一个事务里完成，任何一步抛异常全部回滚，不会留下半更新的库。

写库前会核对 `base_corpus_version`：plan 基于库版本 A 生成、而当前库已是版本 B 时，
直接拒绝执行，要求重新生成报告。这是为了防止「看着旧报告、写进新库」导致误覆盖。
确实需要用旧报告覆盖时加 `--force`。

### 第九步·自检

对每部涉及的法规检查：

- 生效版本的条文数与版本记录里的 `article_count` 是否一致
- 版本内是否有重号（同条号同后缀出现多次）
- 是否有空正文

任何一项不过，命令输出「自检：发现问题」并列出具体法规，不会静默通过。

## 增量包与发版

```
make_delta.py   → delta-<版本>.json     只含本次变更的法规（新增/修订的完整条文 + 失效条号）
apply_delta.py  → 老用户应用，事务执行，应用后与源库逐条比对一致
make_release.py → 全量库 .db.zst + 增量包 .json.zst + manifest.json（含 sha256）
```

老用户升级路径：下增量包（几 KB 到几 MB）→ `apply_delta.py` → 秒级完成。
新用户路径：下全量库（数百 MB 级）→ 解开即用。

Release tag 建议统一为 `data-YYYY.MM.DD`，保留最近三个版本，旧的标记 deprecated 而非删除，
避免老用户手上的 manifest 指向空。

## 已知限制

如实列出来，避免误用：

1. **整部法规废止没有独立入口。** 当前模型能表达「某版本被新版本取代」，
   但「这部法规整体废止、无新版本替代」需要人工处理：直接改库
   （`law_versions.status` 置 `repealed`、条文置 `repealed` 并补 `repeal_date`），
   或后续版本补一个 `tools/repeal_law.py`。
2. **只认 Markdown 与 doc/docx。** PDF、扫描件需先 OCR 成文本，再转 Markdown。
3. **条号解析有边界。** 条文以「第X条」开头的正文才能自动切分；
   附件、表格、成段不分条的法规（如部分决定、批复）可能解析不全，需要人工核对报告。
4. **法规名靠归一化匹配。** 「中华人民共和国劳动合同法」与「劳动合同法」视为同一部；
   但简称差异过大（如「民诉法」vs「中华人民共和国民事诉讼法」）不会被自动识别为同一部，
   会被当作新法规导入。投放前请按官方全名命名文件。
5. **首次建库默认只保留每部法规的最新版本。** 官方源目录里同一部法规常有多份
   （不同公布日期），`build_sqlite_from_markdown.py` 默认只取最新一份，并在输出里
   报「跳过历史版本 N 份」。需要连历史版本一起进库时加 `--keep-history`，
   此时旧版本条文会标为 `repealed`、失效日取新版施行日，检索默认不返回。
6. **向量索引是按法规局部重建的。** 若手工改了库里条文而没有走流水线，向量库会与 SQLite 脱节，
   此时应整体重建向量库。

## 日常操作速查

```bash
# 只出报告，反复跑都安全
python tools/update.py --inbox inbox/

# 看报告
open reports/changes-*.html

# 确认写库（顺带重建本次法规的向量）
python tools/update.py --apply reports/changes-xxx.plan.json --approve --vectors

# 只跑比对，不写报告
python tools/diff_articles.py --inbox inbox/

# 打完补丁发版
python tools/make_delta.py --plan reports/changes-xxx.plan.json --db data/law_data.db --out dist/delta-x.json
python tools/make_release.py --db data/law_data.db --delta dist/delta-x.json --out-dir dist/
```

---

**壹律从简** · [www.yilvai.com](https://www.yilvai.com) · 作者：网络小菜鸟 · 微信：`pmfumin`
