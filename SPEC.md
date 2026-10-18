# everything-mcp v2 规格（Everything 全能力 + 组合能力）

> 本文件是 v2 的唯一接口契约。实现以本文件为准；发现事实与本文冲突时先改本文再改代码。

## 0. 现状（v1，已上线）

- 包：`everything-mcp/`，入口 `server.py`（任意 cwd）或 `python -m everything_mcp`。
- 通道：Everything 内置 HTTP Server，`http://127.0.0.1:8090/`，仅 loopback。
- 已注册进 DSH profile：`serverName=everything`，显示为 `mcp__everything__*`。
- 现有工具：`everything_search` / `everything_count` / `everything_status` / `everything_export`。
- 现有测试：`tests/test_mcp.py`（26 项全绿）。基线不可回归。

## 1. 已核实的事实（2026-10-18，Everything 1.4.1.1024 + HTTP Server）

### 1.1 HTTP JSON API（`GET /?search=<q>&json=1&...`）

| 参数 | 行为 | 备注 |
|---|---|---|
| `search` | Everything 搜索语法 / 正则（配 `regex=1`） | 必须 URL 编码 |
| `json=1` | 返回 `{"totalResults":N,"results":[...]}` | |
| `count` | 本次返回条数；`count=0` 只返回 total | 实测 `count=1000/2000/5000` 未见截断（total 不足时全返） |
| `offset` | 分页起点，正常生效 | |
| `sort` | `name`/`path`/`size`/`date_modified` 生效；其余静默回退默认 | |
| `ascending` | `0`=降序、`1`=升序 | |
| `path_column=1` | 结果增加 `path` | |
| `size_column=1` | 结果增加 `size`（字符串/字节） | |
| `date_modified_column=1` | 结果增加 `date_modified`（**FILETIME 字符串**，需归一化） | |
| `date_created_column` / `date_accessed_column` / `attributes_column` / `extension_column` / `run_count_column` | **1.4 HTTP 不支持，静默忽略** | 不要暴露为可用参数 |
| `regex=1` / `case=1` / `wholeword=1` | 生效（搜索开关） | |
| `path=1` | **唯一有效的 match-path 开关**（`matchpath=1`、`path=true` 均无效） | 实测 `search=blender&path=1` 620→22931 |
| `path` / `filter` | **无效（被忽略）** | 路径范围必须用搜索语法 `path:"..."`/`parent:"..."` |

- 结果字段：始终有 `type`（`file`/`folder`）、`name`；按列参数增加 `path`/`size`/`date_modified`。
- 文件下载（能力覆盖用）：HTML 结果里文件链接形如
  `http://127.0.0.1:8090/D%3A/software/blender-2.93.18-windows-x64/blender.exe`
  即 `/` + 盘符（URL 编码 `D%3A`）+ 路径；`http_server_allow_file_download=1` 已开启。

### 1.2 已验证可用的搜索语法（1.4.1）

`file:` `folder:` `ext:exe;dll` `size:>100mb` `dm:today` `dm:last7days` `dm:>=2026-10-17` `dm:2026-10-17..2026-10-18` `path:"D:\software"` `parent:"D:\software"`（**必须完整路径**）`nopath:` `regex:` `case:` `wholeword:` `wfn:` `startwith:` `endwith:` `len:>10` `empty:`（空目录）`dupe:`（同名重复，**1.4.1 实测支持**，全盘约 180–240ms，比普通查询慢两个数量级）`*` 通配、`|` OR、`!` NOT、引号短语。

补充语义（2026-10-18 实测，help/README 必须照此写）：
- `size:` 语义（实测，Lead 已裁决为"KB 量化"）：带单位（`kb`/`mb`/`gb`）的数值先换算成 **KB 单位**，再按 **`ceil(size/1024) == N*unitInKB`** 比较——`size:1kb` = 1..1024B；`size:2kb` = 1025..2048B；`size:1mb` = 1047553..1048576B（实测 712 条，而 `size:1048576` 仅 680 条）；**无单位 / `b` 是字节精确比较**（`size:1024` 只命中 1024B）；`size:Nk`/`size:Nm`（无 b）里 k/m 被忽略 = N 字节；`size:0kb`/`size:small`=0。help/README 示例优先用无单位或 range。
- `count` 无上限（1000/2000/5000/10000 均按请求返回；count=0 只回 total；负数/非法→0 条）。
- HTTP 参数 `path=1`（布尔真值）是 **match-path 搜索开关**（`search=blender&path=1` → 620→22931），不是路径过滤；`path=<路径>` 与 `filter=` 都无效。
- `ext:a;b` 只认分号；逗号/通配（`ext:py*`）无效。
- name 排序是 Everything 自然排序（非码点序），测试不要断言严格字典序。

### 1.3 已验证不可用 / 有坑（1.4.1）

- `distinct:` `sort:` `run_count:` `date_run:` `attrib:` → 1.5 特性，1.4 无结果；**禁止在帮助与预设里推荐**。`dupe:` 例外，见 §1.2。
- `dm:last1hour` 这类小时级相对语法不支持（返回 0）；需要任意时间窗时用 `dm:>=<ISO 日期>` 或 `dm:<起>..<止>` 计算后传入；`changes` 工具对 <1 天的窗口必须给 warnings。
- 环境怪事（仅影响"用 Everything 验证文件是否存在"的开发手法，不影响工具运行）：DSH `write` 工具新建/覆盖的文件在 Everything 索引里要等该文件被其它进程（pwsh/python）再写一次才可见；Python/pwsh 创建的文件秒级可见。**测试造数据请用 python/pwsh 写文件，不要用 DSH write 工具后立刻断言索引可见。**
- `dc:`（创建时间）/`da:`（访问时间）/`attrib:` 查询极慢：`index_date_created=0`、`index_date_accessed=0`、`index_attributes=0`（Everything.ini）。工具需检测并给出"如需启用需改 ini + 重建索引"的提示。
- 未知 XYZ: 前缀会被当字面量，容易误判；`everything_help` 必须写明支持矩阵。
- 命名实例（`-instance`/`-config`）无法用 Everything 服务 → 空索引 + 管理员弹窗；生产只复用默认实例。

### 1.4 组合能力依赖

- ripgrep：DSH 自带 `%APPDATA%\npm\node_modules\@deepseek-ai\dsh\node_modules\@vscode\ripgrep-win32-x64\bin\rg.exe`（由 `%APPDATA%` 推导的稳定路径）。运行期探测顺序：`EV_RG_EXE` → 该路径 → `where rg` → 已知候选（VS Code/Codex 等）。找不到时错误信息要可操作。
- Python 3.11 + `mcp` SDK 1.26（FastMCP）；无第三方依赖、不联网。

## 2. 文件与所有权（禁止跨文件写，避免冲突）

| 文件 | Owner | 说明 |
|---|---|---|
| `everything_mcp/http_channel.py` | 查询层 | HTTP 客户端：参数、分页、列、FILETIME→ISO、下载 |
| `everything_mcp/client.py` | 查询层 | 业务层：search/count/status/export/download、路径范围、fields、warnings |
| `everything_mcp/config.py` | 查询层 | 新增 env（见 §6） |
| `everything_mcp/presets.py` | 查询层 | 内置预设 + `EV_PRESETS_FILE`（YAML/JSON）加载与校验 |
| `everything_mcp/help.py` | 查询层 | 能力矩阵常量 + 渲染（文本/结构化） |
| `everything_mcp/server.py` | 查询层 | 工具注册（查询类 + 资源）、在末尾调用 `compose_tools.register` |
| `everything_mcp/compose_tools.py` | 组合层 | 组合工具实现 + `register(mcp, get_client)` |
| `everything_mcp/rg.py` | 组合层 | rg 探测 + 内容搜索封装 |
| `tests/test_mcp.py` | 测试/文档 | 扩展测试（保留 v1 断言） |
| `README.md` / `NOTES.md` / `docs/CAPABILITIES.md` | 测试/文档 | 文档与能力矩阵 |
| `SPEC.md`、profile 配置、最终验收 | Lead | |

**接口契约（组合层只依赖以下 client 接口，查询层必须保证）：**

```python
class EverythingError(Exception): ...

class EverythingClient:
    def __init__(self, config: Config): ...
    def search(self, query: str, *, regex=False, case_sensitive=False, whole_word=False,
               match_path=False, max_results=100, offset=0, sort="", ascending=None,
               path_scope="", folder_only=False, file_only=False,
               fields=None, count_only=False) -> dict: ...
        # 返回：query, effective_query, regex, case_sensitive, whole_word, match_path,
        #       path_scope, sort, ascending, offset, next_offset, max_results,
        #       total, returned, truncated, elapsed_ms, channel, results[], warnings[]
    def count(self, query: str, **same_filters) -> dict:  # {query,total,elapsed_ms,channel,warnings}
    def status(self) -> dict
    def export(self, query, out_path, *, format="csv", max_results=5000, offset=0, **filters) -> dict
    def download(self, path: str, dest: str, *, max_bytes=...) -> dict
    def search_pages(self, query, *, page_size=1000, max_items=..., **filters) -> Iterator[list[dict]]
```

- `path_scope` 由 client 组合进搜索语法：`path:"<scope>"` 且对结果再做客户端前缀过滤（双保险）；`folder_only`/`file_only` 组合 `folder:`/`file:`。
- 所有时间归一化为 ISO 8601 UTC；`size` 为 int；`type`/`extension` 保留。
- 任何"不支持"的参数要进 `warnings`，不得静默。

## 3. 工具面（v2 目标）

### 3.1 查询类

1. **everything_search**（向后兼容扩展）
   `query, regex, case_sensitive, whole_word, match_path, path_scope, folder_only, file_only, sort, ascending, offset, max_results, fields, count_only`
   - `fields` ⊂ `[type,name,path,size,date_modified,extension]`；未请求 path/size/date_modified 时不下发对应列参数（省带宽/省 token）。
   - `count_only=true` → 用 `count=0`，只回 total。
   - 返回含 `next_offset`、`effective_query`、`warnings`。
2. **everything_count**：语义同 search 的 count_only，参数精简。
3. **everything_status**：Everything 版本/进程/HTTP/索引条数/DB 路径与大小/volume 列表/ini 关键索引开关（date_created、date_accessed、attributes、folder_size）/rg 可用性/预设数量/auto_start 状态/HTTP 能力矩阵摘要；`hints[]` 给可操作建议。
4. **everything_help**：返回 `{version, syntax_supported{}, syntax_unsupported{}, http_params{}, tools[{name,purpose,key_params}], examples[], tips[]}`，并附 markdown 文本字段。**这是"全部能力"对模型的自解释入口**。
5. **everything_presets**：`action="list"|"run"`, `name`, 透传 `max_results/offset/path_scope`。内置预设：`recent_modified_7d`、`today_changed`、`large_files_100mb`、`screenshots_7d`、`archives_100mb`、`git_repos`、`node_modules`、`downloads_today`。`EV_PRESETS_FILE` 可覆盖/追加（JSON 或 YAML，字段：name/description/query/regex/path_scope/sort/ascending/max_results/fields）。
6. **everything_export**：`format=csv|jsonl|efu`、`columns`、`offset`、`sort/ascending`、`path_scope`、`max_results`；仅允许写 `EV_EXPORT_ROOT`（默认 workspace）内；返回 rows/bytes/path。
7. **everything_download**：`path`（搜索得到的文件）→ 经 HTTP Server 下载到 `EV_DOWNLOAD_ROOT`（默认 workspace）内，校验大小上限、目标不越权；返回 saved_path/bytes。用于"文件服务"能力覆盖。
8. **MCP 资源**（FastMCP `@mcp.resource`）：
   - `everything://help/syntax` → 能力矩阵 markdown
   - `everything://status` → 当前状态 JSON
   - `everything://presets` → 预设清单
   资源读取失败不得影响工具。

### 3.2 组合类（`compose_tools.py`）

9. **everything_summary**：`query, path_scope, group_by="ext"|"dir"|"type"|"size_bucket"|"month"|"parent", dir_depth=2, top_n=20, max_scan=50000`
   返回 `{total, scanned, truncated_scan, groups:[{key,count,size,total_size}], totals{count,size,min,max}, dir_grouping}`。用分页扫描（page_size≤1000）。
   `group_by="dir"` 的键语义：**有 path_scope 时相对 scope**（直接位于 scope 下的文件 → `(root)`，depth=1 → 直接子目录名，depth=2 → `a\b`；返回 `dir_grouping="relative_to_path_scope"`）；无 scope 时退化为绝对路径尾部 N 段（`dir_grouping="absolute_tail"`）。
10. **everything_duplicates**：`mode="name"|"name_size"|"size"|"hash", query, path_scope, min_size=0, max_groups=50, max_files=5000, hash_algo="md5", max_hash_bytes=268435456`
    - `name`: 同名（大小写不敏感）多文件；**优先用 Everything 1.4.1 的 `dupe:` 搜索**缩小候选（实测全盘 ~200ms），再按 name 分组；`dupe:` 不可用（探测失败/返回异常）时回退分页扫描并给 warning；
    - `name_size`: 同名且同大小（可用 `dupe:` 结果按 name+size 分组，回退分页）；
    - `size`: 同大小（≥min_size）多文件；
    - `hash`: 对 size 候选组做哈希确认（只读，限 max_files / max_hash_bytes，超限给 warnings）。
    返回 groups（size/hash/paths）+ stats + warnings。不得删除/修改任何文件（只读）。
11. **everything_grep**：`pattern, fixed_strings=false, case_sensitive=false, query="", path_scope="", exts=[], globs=[], no_ignore=false, max_results=200, max_filesize="10M", timeout_ms=20000`
    - 若 `path_scope` 为空但 `query` 有结果 → 由候选文件共同父目录推导搜索根（最多 8 个）；仍为空则要求显式 path_scope。
    - 用 rg（`--json` 或 `--line-number --no-heading`）；返回 matches[{path,line,column,text}] + stats{files_searched,files_matched,rg_exe,elapsed_ms,truncated}。
    - rg 缺失：返回可操作错误（安装/设置 EV_RG_EXE），不得崩溃。
12. **everything_batch_stat**：`paths[]`（≤200），`include_hash=false`、`hash_algo`、`max_hash_bytes`；返回每项 `{path,exists,type,size,date_modified,date_created,mode,is_link,hash?,error?}`；不做写操作。
13. **everything_changes**：`since="24h"|"7d"|ISO|"today"`, `path_scope="", exts=[], max_results=200, sort="date_modified", ascending=false`；返回变更文件列表 + `{count, window:{since,until}}`。**实现要求：since 一律归一化为绝对时间并拼 `dm:>=YYYY-MM-DD`（本地日期），客户端再按 [since, now] 精确过滤**；1.4 不支持 `dm:lastNh`（小时级），返回里 `total` 是日期级候选数、`count` 是精确窗内数，必须在 warnings 中说明日期级精度。
14. **everything_reveal**：`path`（文件/目录）→ `explorer.exe /select,<path>` 或打开目录；`EV_ALLOW_SIDE_EFFECTS=0` 时直接拒绝并给提示；返回 ok/opened。只做 reveal/open，**不做删除/移动/复制/重命名**。

## 4. 环境变量（config.py）

| 变量 | 默认 | 说明 |
|---|---|---|
| `EV_HTTP_HOST/PORT/TIMEOUT` | 127.0.0.1 / 8090 / 2.0 | 既有 |
| `EV_EVERYTHING_EXE` | 自动探测 | 既有 |
| `EV_AUTO_START` | 1 | 既有 |
| `EV_MAX_RESULTS_CAP` | 1000 | 既有 |
| `EV_WORKSPACE_ROOT` / `EV_EXPORT_ROOT` | cwd | 既有 |
| `EV_DOWNLOAD_ROOT` | =workspace | 下载落盘根 |
| `EV_RG_EXE` | 自动探测 | ripgrep 路径 |
| `EV_PRESETS_FILE` | 空 | 自定义预设文件（必须在 workspace 内或显式绝对路径；相对 EV_WORKSPACE_ROOT） |
| `EV_ALLOW_SIDE_EFFECTS` | 1 | reveal/open 开关 |
| `EV_SCAN_CAP` | 50000 | 聚合扫描上限（行） |
| `EV_HASH_MAX_BYTES` | 268435456 | 哈希总量上限 |
| `EV_LOG_LEVEL` | INFO | 既有 |

## 5. 非功能要求

- 所有工具：超时保护、结果上限、错误信息可操作（中文）、不把整盘路径无上限塞回模型。
- 只读优先：除 `everything_reveal`（打开资源管理器）与 `everything_download`/`everything_export`（写 workspace 内文件）外，不得修改文件系统。
- 性能：普通搜索 P95 < 300ms（热）；count < 100ms；summary/duplicates 在 max_scan 内 < 5s；grep 受 timeout 限制。
- 兼容性：v1 的 4 个工具名称与参数保留；v1 测试断言不得删改（可新增）。
- 代码风格：Python 3.11、type hints、无第三方依赖；日志只写 stderr（不污染 stdio）。

## 6. 验收（Lead 最终执行）

1. `python everything-mcp/tests/test_mcp.py` 全绿（含 v1 26 项 + v2 新增 ≥18 项）。
2. 每个新工具至少 1 条真实数据验证（3,201,0xx 条索引的实测环境）：search 分页/排序/fields/count_only、status 能力矩阵、help、presets、export 三格式、download、summary（ext/dir）、duplicates（name/size/hash）、grep（真实代码库）、batch_stat、changes、reveal（可用 EV_ALLOW_SIDE_EFFECTS=0 验证拒绝路径）。
3. DSH 端：`kill` 当前 MCP server 进程（python 子进程）→ 自动重连加载新代码 → `mcp__everything__everything_help` 等新工具可见可用。
4. 更新 profile env：`EV_RG_EXE`、`EV_PRESETS_FILE`（可选）、其余默认即可。
5. 文档：README 工具全表 + 示例 + 能力矩阵链接；NOTES 记录 1.4/1.5 差异与升级建议。
