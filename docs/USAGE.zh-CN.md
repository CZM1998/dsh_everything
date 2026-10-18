# 使用手册

`dsh_everything` 的面向任务配方：调用什么、按什么顺序、响应应该长什么样。安装见
[README](../README.zh-CN.md)；经过验证的查询矩阵见 [CAPABILITIES.md](CAPABILITIES.md)。

[English](USAGE.md) | **简体中文**

- 在 DSH 中，每个工具名都带前缀 `mcp__everything__`
  （`mcp__everything__everything_search`）。本文为可读性使用不带前缀的名字。
- 所有参数都是传给工具的 JSON 对象。示例中的路径、大小与耗时均为示意 —— 使用你自己的
  路径。
- 响应已裁剪到关键字段。真实响应还会包含 `effective_query`、`channel`、
  `elapsed_ms`、`stats` 与 `warnings`。
- 出现任何异常前先调用 `everything_status`；对查询语法不确定时调用
  `everything_help`。

## 目录

- [约定](#约定)
- [配方](#配方)
  - [1. 按名称片段查找文件](#1-按名称片段查找文件)
  - [2. 在单个项目内搜索](#2-在单个项目内搜索)
  - [3. 按类型、大小和日期过滤](#3-按类型大小和日期过滤)
  - [4. 存在性判断与计数](#4-存在性判断与计数)
  - [5. 最大的 N 个文件](#5-最大的-n-个文件)
  - [6. 按扩展名聚合](#6-按扩展名聚合)
  - [7. 按目录聚合](#7-按目录聚合)
  - [8. 重复文件](#8-重复文件)
  - [9. 用 ripgrep 搜索内容](#9-用-ripgrep-搜索内容)
  - [10. 最近变更](#10-最近变更)
  - [11. 导出为 CSV / JSONL / EFU](#11-导出为-csv--jsonl--efu)
  - [12. 下载文件](#12-下载文件)
  - [13. 在资源管理器中定位路径](#13-在资源管理器中定位路径)
  - [14. 预设](#14-预设)
  - [15. 批量元数据与哈希](#15-批量元数据与哈希)
- [Agent prompt 模式](#agent-prompt-模式)
- [故障排查场景](#故障排查场景)

## 约定

| 概念 | 约定 |
|---|---|
| 工作区 | `<WORKSPACE>` 是 Agent 工作目录（`EV_WORKSPACE_ROOT`）。示例用 `D:\work`。 |
| 仓库 | `<PATH-TO-REPO>` 是本项目的克隆位置。 |
| 分页 | 把返回的 `next_offset` 作为下一次的 `offset`；`truncated=false` 时停止。 |
| 字段裁剪 | 只请求你需要的 `fields` —— 省 token，也跳过不必要的 Everything 列。 |
| 先计数 | 对昂贵的组合工具，先调 `everything_count` 估算任务规模。 |
| 只读 | 只有 `everything_export`、`everything_download` 与 `everything_reveal` 有副作用。 |

## 配方

### 1. 按名称片段查找文件

**场景：** 用户记得名字的一部分，但不知道位置。

**调用**

```json
{
  "query": "\"Q3 budget\" ext:xlsx",
  "sort": "date_modified",
  "ascending": false,
  "max_results": 20,
  "fields": ["name", "path", "size", "date_modified"]
}
```

**预期形状**

```json
{
  "total": 3,
  "returned": 3,
  "truncated": false,
  "next_offset": 3,
  "results": [
    {"type": "file", "name": "Q3-budget-2026.xlsx",
     "path": "D:\\work\\reports\\Q3-budget-2026.xlsx",
     "size": 48213, "date_modified": "2026-10-12T08:21:04Z"}
  ],
  "warnings": []
}
```

**说明：** 引号强制短语匹配；不加引号时多个词是 AND。`wfn:` 匹配完整文件名，
`ext:` 用分号分隔多个扩展名。用户不知道扩展名时就去掉 `ext:` —— 不要猜。

### 2. 在单个项目内搜索

**场景：** 范围比模式更重要。

**调用**

```json
{"query": "ext:ts", "path_scope": "D:\\work\\my-app", "max_results": 5}
```

**预期形状**

```json
{
  "total": 214,
  "returned": 5,
  "truncated": true,
  "next_offset": 5,
  "results": [{"name": "server.ts", "path": "D:\\work\\my-app\\src\\server.ts"}]
}
```

**说明：** `path_scope` 会作为 `path:"..."` 加入 Everything 查询，**并且**在客户端
二次校验，因此结果不会越出范围。优先用 `path_scope` 而不是把 `path:` 塞进 `query` ——
模型更容易用对。

### 3. 按类型、大小和日期过滤

**场景：** “大的旧日志”、“最近的安装包”、“大视频文件”。

**调用**

```json
{
  "query": "ext:log size:>10mb dm:>=2026-09-01",
  "sort": "size",
  "ascending": false,
  "max_results": 20,
  "fields": ["name", "path", "size", "date_modified"]
}
```

**预期形状**

```json
{
  "total": 137,
  "results": [
    {"name": "build.log", "path": "D:\\work\\builds\\build.log",
     "size": 53485308, "date_modified": "2026-09-14T06:18:42Z"}
  ]
}
```

**说明：** `size:` 带 `kb/mb/gb` 时是 KB 量化 —— 见
[README §7](../README.zh-CN.md#7-everything-查询语法速查)。精确阈值请用字节：
`size:>10485760`。`dm:` 是修改时间；`dc:` 与 `da:` 默认未建索引，非常慢。

### 4. 存在性判断与计数

**场景：** 只需要“有没有”或“有几个” —— 最省的调用。

**调用**

```json
{"query": "wfn:important.docx"}
```

```json
{"query": "ext:py", "path_scope": "D:\\work\\my-app", "folder_only": false}
```

**预期形状**

```json
{"query": "wfn:important.docx", "total": 2, "elapsed_ms": 3.1, "channel": "http"}
```

**说明：** `everything_count` 不返回文件路径，因此适合在任何读取或处理之前做决策
（`total > 0`）。

### 5. 最大的 N 个文件

**场景：** 磁盘空间审计。

**调用**

```json
{
  "query": "size:>500mb",
  "sort": "size",
  "ascending": false,
  "max_results": 20,
  "fields": ["name", "path", "size"]
}
```

**预期形状**

```json
{
  "total": 96,
  "results": [
    {"name": "image.iso", "path": "D:\\work\\images\\image.iso", "size": 4294967296}
  ]
}
```

**说明：** `sort=size&ascending=false` 让最大的排最前。加 `ext:iso` 或
`path_scope` 收窄范围。如果需要总字节数而不是列表，用 `everything_summary` 的
`group_by=size_bucket`（或对返回的 size 求和）。

### 6. 按扩展名聚合

**场景：** “这里都有哪些类型的文件 / 什么在占空间？”

**调用**

```json
{"query": "", "path_scope": "D:\\work\\my-app", "group_by": "ext", "top_n": 10}
```

**预期形状**

```json
{
  "total": 2000,
  "scanned": 2000,
  "truncated_scan": false,
  "groups": [
    {"key": "py", "count": 421, "size": 1820011, "min_size": 120, "max_size": 88213, "avg_size": 4323},
    {"key": "md", "count": 88, "size": 441002, "min_size": 210, "max_size": 9120, "avg_size": 5011}
  ],
  "totals": {"count": 2000, "size": 4200000, "min": 0, "max": 91200}
}
```

**说明：** 默认只聚合文件（目录的 `size` 没有意义）。`truncated_scan=true` 表示数字
只是下限 —— 调大 `max_scan`（受 `EV_SCAN_CAP` 限制，默认 50000）或收窄范围。

### 7. 按目录聚合

**场景：** 找出哪个子目录占用最多。

**调用**

```json
{
  "query": "",
  "path_scope": "D:\\work\\my-app",
  "group_by": "dir",
  "dir_depth": 1,
  "top_n": 20
}
```

**预期形状**

```json
{
  "groups": [
    {"key": "(root)", "count": 12, "size": 22000},
    {"key": "src", "count": 180, "size": 920000},
    {"key": "node_modules", "count": 900, "size": 3100000}
  ],
  "dir_grouping": "relative_to_path_scope"
}
```

**说明：** 有 `path_scope` 时键相对于它；没有时是绝对路径的尾部。`dir_depth` 取值
1–12。`group_by=parent` 改为按直接父目录分组。

### 8. 重复文件

**场景：** 清理下载、照片、备份。

**调用（快速信号）**

```json
{"mode": "name_size", "path_scope": "D:\\work\\photos", "min_size": 1048576, "max_groups": 10}
```

**调用（确认）**

```json
{"mode": "hash", "query": "", "path_scope": "D:\\work\\photos",
 "min_size": 1048576, "hash_algo": "sha256", "max_hash_bytes": 536870912}
```

**预期形状**

```json
{
  "mode": "name_size",
  "groups": [
    {"key": "IMG_0001.jpg|2048", "count": 2, "size": 2048, "path_count": 2,
     "paths": ["D:\\work\\photos\\backup\\IMG_0001.jpg",
               "D:\\work\\photos\\IMG_0001.jpg"]}
  ],
  "stats": {"groups_found": 1, "returned_groups": 1, "dupe_support": true},
  "warnings": []
}
```

**说明：** `name_size` 是快速启发式；`hash` 是确定性判断但要读字节。每组只返回前
`max_paths_per_group` 条路径（默认 2）；真实数量看 `path_count`。工具从不删除任何
东西 —— 删除是调用方的决定，并且应先得到用户确认。

### 9. 用 ripgrep 搜索内容

**场景：** 搜索文件*内部*（`TODO`、错误字符串、API key、配置键）。

**调用**

```json
{
  "pattern": "TODO|FIXME",
  "path_scope": "D:\\work\\my-app",
  "exts": ["py", "ts"],
  "max_results": 50,
  "case_sensitive": false
}
```

**预期形状**

```json
{
  "matches": [
    {"path": "D:\\work\\my-app\\backend\\server.py", "line": 42,
     "column": 5, "text": "# TODO: cache this"}
  ],
  "stats": {"files_searched": 214, "files_matched": 3, "elapsed_ms": 118.2,
            "truncated": false, "timed_out": false}
}
```

**说明：** 这是 ripgrep，不是 Everything 索引。用 `exts`、`globs`
（`["!vendor/**"]`）或 `path_scope` 收窄候选集。`fixed_strings=true` 按字面文本
匹配。URL 这类模式要么转义正则字符，要么用 `fixed_strings`。

### 10. 最近变更

**场景：** “今天/本周改了什么”、站会摘要、检查生成产物。

**调用**

```json
{"since": "7d", "path_scope": "D:\\work\\my-app", "max_results": 50}
```

**预期形状**

```json
{
  "items": [
    {"name": "server.py", "path": "D:\\work\\my-app\\backend\\server.py",
     "date_modified": "2026-10-18T03:15:07Z", "extension": "py"}
  ],
  "count": 12, "total": 33,
  "window": {"since": "2026-10-11T03:27:59Z", "until": "2026-10-18T03:27:59Z",
             "spec": "7d", "dm_expression": "dm:>=2026-10-11", "precise": true},
  "warnings": ["<说明 dm: 过滤为日期级、精确时间窗由客户端二次过滤>"]
}
```

**说明：** `total` 是日期级候选数；`count` 是精确数量。`since` 接受 `today`、
`24h`、`7d`、`30m`、`last7days` 或 ISO 时间戳。默认按修改时间倒序。

### 11. 导出为 CSV / JSONL / EFU

**场景：** 把结果交给人类、表格软件或其他工具。

**调用**

```json
{
  "query": "ext:log size:>10mb",
  "out_path": "reports/big_logs.csv",
  "format": "csv",
  "sort": "size",
  "ascending": false,
  "max_results": 5000
}
```

**预期形状**

```json
{"ok": true, "format": "csv", "path": "D:\\work\\reports\\big_logs.csv",
 "rows": 137, "bytes": 24512, "total": 137, "truncated": false}
```

**说明：** 路径相对于 `EV_EXPORT_ROOT` 解析，之外的路径会被拒绝。CSV 是带 BOM 的
UTF-8（Excel 友好）。`jsonl` 每行一个 JSON 对象；`efu` 是 Everything 文件列表。
默认 5000 行，上限 10000 —— 需要更多时用 `offset` 分页。把保存路径返回给用户，不要
把几千行贴进对话。

### 12. 下载文件

**场景：** Agent 需要文件本体（在工作区内），而不只是路径。

**调用**

```json
{"path": "D:\\work\\builds\\release.zip", "dest": "artifacts/release.zip"}
```

**预期形状**

```json
{"ok": true, "source": "D:\\work\\builds\\release.zip",
 "saved_path": "D:\\work\\artifacts\\release.zip",
 "bytes": 33554432, "max_bytes": 2147483648}
```

**说明：** 需要 `http_server_allow_file_download=1`。`dest` 相对于
`EV_DOWNLOAD_ROOT` 解析；重名会自动加数字后缀。`max_bytes` 默认 2 GiB。404 意味着
文件已移动或索引过期 —— 先重新搜索。

### 13. 在资源管理器中定位路径

**场景：** 用户就在机器前，想直接打开文件。

**调用**

```json
{"path": "D:\\work\\reports\\Q3-budget-2026.xlsx"}
```

**预期形状**

```json
{"ok": true, "opened": true, "mode": "select", "path": "D:\\work\\reports\\Q3-budget-2026.xlsx"}
```

**说明：** 这是唯一的副作用工具。只读部署（`EV_ALLOW_SIDE_EFFECTS=0`）下它返回
`ok=false` 并给出提示，不启动任何程序。不要在循环中使用。

### 14. 预设

**场景：** 重复查询，或用户自定义的“常用清单”。

**调用**

```json
{"action": "list"}
```

```json
{"action": "run", "name": "large_files_100mb", "max_results": 30}
```

**预期形状**

```json
{
  "action": "run",
  "name": "large_files_100mb",
  "preset": {"query": "size:>100mb", "sort": "size", "ascending": false},
  "result": {"total": 96, "returned": 30, "results": []},
  "warnings": []
}
```

**说明：** 服务器自带 8 个内置预设；用 `EV_PRESETS_FILE` 添加自己的（见
[README §8.8](../README.zh-CN.md#88-自定义预设)）。预设支持 `{downloads}`、
`{userprofile}`、`{workspace}` 占位符。

### 15. 批量元数据与哈希

**场景：** 搜索之后，检查 N 个路径是否存在、大小以及两个文件是否相同。

**调用**

```json
{
  "paths": ["D:\\work\\a\\report.txt", "D:\\work\\b\\report.txt", "D:\\work\\gone.txt"],
  "include_hash": true,
  "hash_algo": "sha256"
}
```

**预期形状**

```json
{
  "items": [
    {"path": "D:\\work\\a\\report.txt", "exists": true, "size": 2048,
     "hash": "9f2c…", "date_modified": "2026-10-12T08:21:04Z"},
    {"path": "D:\\work\\gone.txt", "exists": false, "error": "not found"}
  ],
  "exists_count": 2, "missing_count": 1
}
```

**说明：** 每次最多 200 个路径（超出会截断并告警）。哈希只读，受 `max_hash_bytes`
预算限制。用它替代 N 次单独 shell `stat` 调用。

## Agent prompt 模式

以下是用户可以给 DSH Agent 的 prompt，以及 Agent 应遵循的工具序列。为可读性省略了
工具前缀。

### “找到 Q3 报告并总结一下”

1. `everything_search`，参数 `{"query": "\"Q3 report\"", "sort": "date_modified", "ascending": false, "max_results": 10}`。
2. 如有多个命中，选最新的 `.docx`/`.xlsx`/`.md`；如无命中，改用 `ext:` 变体或更宽松
   的短语重试。
3. 用常规文件读取工具读取选中的文件，然后总结。
4. 回答时给出精确路径与摘要。文件有歧义时列出候选并只问一个澄清问题。

### “这个项目今天改了什么？”

1. `everything_changes`，参数 `{"since": "today", "path_scope": "<PROJECT>", "max_results": 50}`。
2. 在回答中按目录或扩展名对返回结果分组。
3. 如果用户关心*内容*变化，再用 `everything_grep` 搜相关文件，或用常规 shell 工具看
   差异。

### “清理我照片目录里的重复文件”

1. `everything_count`，参数 `{"query": "", "path_scope": "D:\\work\\photos"}` 估算规模。
2. `everything_duplicates`，参数 `{"mode": "name_size", "path_scope": "D:\\work\\photos", "min_size": 1048576}`。
3. 对候选组，若用户需要确定性，升级到 `{"mode": "hash", "hash_algo": "sha256"}`。
4. 展示分组与拟删除清单；**未经明确确认绝不删除**。`everything_duplicates` 本身只读。

### “找出 API key 被使用的所有地方”

1. 如果用户知道文件类型，先用 `everything_count` 加宽泛的名称过滤；然后
2. `everything_grep`，参数 `{"pattern": "API_KEY", "path_scope": "<PROJECT>", "exts": ["py", "ts", "js", "env"], "max_results": 100}`。
3. 报告每处匹配的 file:line；不要把密钥值打印到对话里。
4. 如果 ripgrep 缺失，转述可操作的错误（安装或设置 `EV_RG_EXE`）。

### “把所有超过 10MB 的构建日志导出来”

1. `everything_count`，参数 `{"query": "ext:log size:>10mb"}` 确认规模。
2. `everything_export`，参数 `{"query": "ext:log size:>10mb", "out_path": "reports/big_logs.csv", "format": "csv", "max_results": 5000}`。
3. 若 `truncated=true`，用 `offset` 导出后续分片（单次导出上限 10000）。
4. 返回保存路径与行数，而不是行内容。

### “我的磁盘为什么满了？”

1. `everything_summary`，参数 `{"query": "", "path_scope": "D:\\", "group_by": "ext", "top_n": 15, "max_scan": 50000}`。
2. `everything_search`，参数 `{"query": "size:>500mb", "sort": "size", "ascending": false, "max_results": 20}`。
3. 可选 `{"group_by": "size_bucket"}` 得到直方图。
4. 总结主要占用项；数字是下限时注明 `truncated_scan`。

## 故障排查场景

### A. 所有工具都报错，或 `status` 说端点不可达

1. 调 `everything_status`，看 `http.reachable`、`http.error`、
   `everything.running` 与 `hints`。
2. 若 `http.reachable=false` 但 `everything.running=true`：HTTP 未启用、端口不对或
   绑定写错。重新核对 ini。记住：`http_server_bindings` 必须是**裸 IP** ——
   `127.0.0.1:8090` 会静默失败。
3. 若 `everything.running=false`：启动 Everything，或设置 `EV_AUTO_START=1`。
4. 若端口有响应但 `everything.running=false`，说明 8090 被其他程序占用：
   `netstat -ano | findstr :8090`，然后同时修改 ini 端口与 `EV_HTTP_PORT`。

### B. 查询成功但返回空

1. `everything_status` → `index.total_items`。为 `0` 说明索引仍在构建，或命名实例没有
   服务支持的索引；等待或改用默认实例。
2. 试一个最简单的名称搜索（`{"query": "*.exe", "max_results": 1}`）。如果它正常，
   问题是查询语义，不是索引。
3. 在 `everything_help` 核对前缀：`distinct:`、`sort:`、`run_count:`、`date_run:` 是
   1.5 特性，在 1.4 返回 0。`ext:a,b` 与 `ext:py*` 同样返回 0 —— 用分号与精确扩展名。
4. 如果是 `size:` 查询，记得 KB 量化
   （[README §7.3](../README.zh-CN.md#73-size--kb-量化的坑)）。

### C. `everything_grep` 找不到 ripgrep

1. 错误信息会列出尝试过的 `env`、`dsh-bundled`、`PATH` 与候选路径。
2. 安装 ripgrep（`winget install BurntSushi.ripgrep.MSVC`），或把 `EV_RG_EXE` 指向
   已有的 `rg.exe`。
3. 重启 MCP 服务器（或 DSH），让新的环境变量生效。
4. 其他工具不受影响 —— 可以继续用 `everything_search`。

### D. `everything_download` 失败

1. 404 → 文件已移动、被删除，或索引过期。重新 `everything_search`，用新路径重试。
2. 403 / 无内容 → 未设置 `http_server_allow_file_download=1`。
3. “too large” → 调大 `max_bytes`（默认 2 GiB）或换传输方式。
4. “outside the download root” → `dest` 解析到 `EV_DOWNLOAD_ROOT` 之外；改用其下的
   相对路径。
5. 超时 → 调大 `EV_HTTP_TIMEOUT` 后重试一次。

### E. 刚写入的文件找不到

Everything 通过 NTFS USN journal 在数秒内更新，但不是即时。等 1–5 秒再试。不要设计
依赖“索引零延迟读写一致性”的工作流；已经知道路径时直接读文件。

### F. 导出或下载被拒：工作区外

目标解析到 `EV_EXPORT_ROOT` / `EV_DOWNLOAD_ROOT` 之外。改用相对路径（会相对根目录
解析），或修改环境变量。这个检查是刻意的：副作用工具不能写到磁盘任意位置。

### G. `everything_reveal` 拒绝执行

设置了 `EV_ALLOW_SIDE_EFFECTS=0`。该变量每次调用重新读取：设为 `1` 后重试即可，
无需重启。如果路径不存在，工具会返回可操作错误，而不是打开资源管理器。
