# 数据 2026.11.08.5（app 2.4.0）

法规 26,788 部（现行口径），全库条文 1,383,142 条，版本 34,118 个。manifest 的 `article_count=1,038,922`（非废止口径）。

## 本版变更

- 规章库整体入库：部门规章 2,388 版 / 92,603 条，地方政府规章 7,631 版 / 245,918 条
- 相对 11.08.4：版本 +10,019，条文 +338,521；存量法规类型数量不变
- schema 4 → 5（`law_versions` 新增 `order_no`/`source_url`/`source_gap` 三个可空列，向后兼容）
- 修复 G3 生效区间：删除重复版 2 个（外商投资安全审查办法 24689、水生野生动物价值评估办法 24871），另 2 部公布日缺失按施行日期排序判定（网络安全审查办法、渔业捕捞许可管理规定），未猜公布日
- 修复 G4 页尾残渣：清洗 5 条「扫一扫在手机打开当前页」；「责任编辑」26 条经核为合法法律术语，保留并降为告警
- 修复 G7 切分登记：amendment 豁免 8 版、source_long 21 条、source_gap 10 个，均附源 md 证据，未虚构条文

## 资产（6 个）

| 文件 | 字节 | sha256 |
|---|---|---|
| `law_data-2026.11.08.5.db.zst` | 1,024,644,211 | `6c3fc351aa60f1ab4e48489a8f928a64342cb1b4401b0bf4c34bb908f2951f82` |
| `law_data-2026.11.08.5.db.gz` | 1,308,120,228 | `0653b7606e937d3fdb05a5b6f2a21fef73d56216425fa192c88c268d1244d0cb` |
| `delta-2026.11.08.5.json` | 486,758,199 | `dd5f8653f6e026f37cac84f192d3e65e2678632e12b45f2095e3ad30b22fccaa` |
| `delta-2026.11.08.5.json.zst` | 33,447,651 | `6a257b38c14dcc1414cd52a6cfe1aed765eb13cb7e5b096e3c72316c20e71851` |
| `delta-2026.11.08.5.json.gz` | 54,998,698 | `cfcc8c9a9aa424e0753ee3cf07d69c909106153a988e50fc5f1c1db7d57dcd56` |
| `manifest.json` | 1,406 | `68e1761c4e4e185c4bf01ca33d0bbe82981152eb9603ffc5929e082b8c1254a0` |

解压后整库 `db_sha256 = f0727cece70265d281fdc4dbd9f82a2a099775896e157a3956fd8bbbafc43b83`。

## 增量包

`min_version=2026.11.08.4`，含 10,011 版 / 338,521 条正文，另 45 部整部废止标记。本地版本 ≥ 11.08.4 可只补增量，更早回落全量。

## 升级

```bash
python3 tools/check_update.py --update
# 或 ./update.sh
```

## 回滚

```bash
pkill -f "server.law_search_service"
cp -c data/law_data.db data/law_data.db.rollback-$(date +%Y%m%d)
# 恢复上一版线上库（2026.11.08.4）
```
