# Everything 能力矩阵（1.4.1.1024 实测）

> 本文是 `everything-mcp` v2 的**能力事实来源**：在 2026-10-18 用 Everything
> 1.4.1.1024 的内置 HTTP Server（`127.0.0.1:8090`）逐条实测，记录「支持 / 不支持 /
> 慢查询 / 1.5 特性」与可操作建议。`SPEC.md` §1 与本文冲突时以本文实测结果为准，
> 并同步更新 SPEC。
>
> 配套测试：`tests/test_mcp.py`（v1 26 项 + v2 新增 ≥18 项，全部走真实 HTTP 索引）。
> `everything_help` 工具返回的 `syntax_supported` / `syntax_unsupported` 应与本文一致。

- 测试环境：Windows 11 / Everything **1.4.1.1024**（默认实例，`-startup`）/
  索引 **约 3,201,1xx 条** / Python 3.11 / HTTP `http://127.0.0.1:8090/` 仅 loopback。
- 关键 ini（`%APPDATA%\Everything\Everything.ini`，最小片段见
  [everything.ini.example](everything.ini.example)）：`http_server_enabled=1`、
  `http_server_port=8090`、`http_server_bindings=127.0.0.1`、`http_server_allow_file_download=1`、
  `index_size=1`、`index_date_modified=1`、`index_date_created=0`、`index_date_accessed=0`、
  `index_attributes=0`。
- 实测方式：`GET /?search=<q>&json=1&count=N&...`，`urllib`（显式禁用代理）；
  每条命令都可直接重放，`count=1` 时只看 `totalResults`，`count>=5` 时看 `results`。

## 0. 速览（TL;DR）

| 能力 | 结论 |
|---|---|
| 普通搜索（名称/路径/扩展名/size/dm/path/parent/通配/OR/NOT/引号/中文） | ✅ 可用，热查询 **0.7~10ms** |
| `size:` 单位语义 | ⚠️ 带 `kb/mb/gb` 时按**向上取整的 KB 值**量化比较（`size:<1kb` 只命中 0B）；精确字节用无单位/`b`（见 §2.2） |
| `count` 上限 | **无上限**：1000/2000/5000/10000/20000 均按请求返回；`count=0` 只回 total |
| `offset` 分页 | ✅ 稳定、页间不重叠；负数/非法值按 0 处理 |
| `sort` | 仅 `name`/`path`/`size`/`date_modified` 真正生效，其余**静默回退默认（name 升序）** |
| `ascending` | `1`=升序、`0`=降序；**省略时也是升序**（注意与 Everything GUI 的列默认方向不同） |
| 结果列 | 仅 `path_column`/`size_column`/`date_modified_column` 生效；其余列参数静默忽略 |
| `regex`/`case`/`wholeword`/`path=1`（match-path） | ✅ 生效；`path=1` 会慢 5~10 倍（全路径匹配） |
| `path=<路径>` / `filter=` 作为过滤器 | ❌ 无效；路径范围必须写进 `search` 语法（`path:"..."` / `parent:"..."`） |
| 文件下载（HTTP） | ✅ `GET /D%3A/...` 返回 200 + Content-Length，sha256 与本地一致 |
| `dupe:`（同名重复） | ✅ **1.4.1 实测支持**，但全盘约 **180~240ms**（比普通查询慢两个数量级） |
| `distinct:` / `sort:` / `run_count:` / `date_run:` | ❌ 1.5 特性，1.4 返回 0；`attrib:` 属 1.4 语法但极慢 |
| `dc:`（创建时间）/ `da:`（访问时间）/ `attrib:` | ⚠️ **慢查询**：>5s 超时（未建索引，全盘扫描） |
| 实时索引更新 | ✅ python/pwsh 新建/修改/删除后 1~5s 可见；⚠️ **DSH `write` 工具写的文件不可见**，见 §5 |
| 中文路径/文件名 | ✅ 正常（HTTP 链路 UTF-8） |

## 1. HTTP JSON API 参数矩阵

响应结构（UTF-8 JSON）：

```json
{"totalResults": 7, "results": [{"type": "file", "name": "Everything.exe",
  "path": "C:\\Program Files\\Everything", "size": "2265096",
  "date_modified": "133295352000000000"}]}
```

- `totalResults`：未截断的命中总数（int）。
- 结果字段：**始终**有 `type`（`file`/`folder`/`drive`）与 `name`；其余按列参数出现。
- `path` 是**父目录**（不含 name），需自行拼接；`size` 是**字符串**；`date_modified`
  是 **Windows FILETIME 字符串**（1601-01-01 起的 100ns 数），必须归一化为 ISO 8601。
- 基准命令（空查询取索引总数，5.5ms）：
  `GET /?search=&json=1&count=1` → `totalResults=3201114`。

### 1.1 `count` / `offset`

| 参数 | 行为 | 实测 |
|---|---|---|
| `count=N` | 本次最多返回 N 条；`totalResults` 不受影响 | `search=ext:exe`（total=26827）：`count=1000→1000`、`2000→2000`、`5000→5000`、`10000→10000`、`20000→20000`（1.2/1.7/2.8/5.6/71ms） |
| `count=0` | 只回 `totalResults`，`results=[]` | `search=everything.exe&count=0` → total=7、n=0、26.7ms |
| `count` 负数/非法 | 返回 0 条，但 total 正常 | `count=-1`/`count=abc` → n=0、total=26827 |
| `offset=N` | 分页起点 | `everything.exe`（total=7）：offset=0→5 条、offset=5→2 条、offset=100→0 条 |
| `offset` 负数/非法 | 按 0 处理 | `offset=-1` → 5 条；`offset=abc` → 从第 0 条开始 |
| 分页一致性 | 页间不重叠、顺序稳定 | `ext:exe&sort=name&ascending=1&count=10&offset=0/10/20` → 30 条 `(name,path)` 去重后仍 30 条 |

> 结论：HTTP 层 **没有 count 上限**（20000 已实测）；`EV_MAX_RESULTS_CAP=1000`
> 是本项目**主动**为 token/带宽设的上限，不是 Everything 限制。

### 1.2 `sort` / `ascending`

| 值 | 是否生效 | 实测 |
|---|---|---|
| `name` | ✅ | `ext:exe` 前 10 条按自然排序（Everything 自然排序，**非码点序**，见 §3.4） |
| `path` | ✅ | `*` 前 10 条：`C:`、`D:`、`$Recycle.Bin`…（按完整路径） |
| `size` | ✅ | `ext:exe` 默认升序 → 0 字节文件在前；`ascending=0` → 最大文件在前（1.1GB→…） |
| `date_modified` | ✅ | 升序从 2007 年文件开始；`ascending=0` → 2026 年最新 |
| `date_created` / `date_accessed` / `extension` / `type` / `run_count` / `date_run` / 任意字符串 | ❌ 静默回退默认（name 升序） | 与不传 `sort` 的结果逐条一致（`ext:exe`、`*`、混合扩展名查询均验证） |

```text
GET /?search=blender&json=1&count=5&sort=date_modified&ascending=0
  → 2026 年最新修改的 blender 相关文件
```

- `ascending=1` 升序、`0` 降序；**省略 `ascending` 时表现为升序**（含 `sort=size`/`sort=date_modified`）。
- `ascending=2` 等非法值按省略处理。
- 只在白名单里暴露 4 个 sort；其它值必须进 `warnings`（本项目 `client` 已如此处理）。

### 1.3 结果列参数

| 参数 | 是否生效 | 结果新增字段 | 实测 |
|---|---|---|---|
| `path_column=1` | ✅ | `path`（父目录字符串） | `everything.exe` 2 条 → keys `name,path,type` |
| `size_column=1` | ✅ | `size`（**字符串**字节数） | keys `name,size,type`，如 `"2265096"` |
| `date_modified_column=1` | ✅ | `date_modified`（**FILETIME 字符串**） | `"133295352000000000"` → ISO `2023-05-26T00:40:00Z` |
| `date_created_column` / `date_accessed_column` / `attributes_column` / `extension_column` / `run_count_column` | ❌ 静默忽略 | 无新增字段 | 逐个实测：keys 仍为 `name,type` |

- 三列同时请求 → keys = `date_modified,name,path,size,type`（与请求顺序无关）。
- 本项目 `fields` 只暴露 `type/name/path/size/date_modified/extension`；未请求的列**不下发**，
  省带宽/省 token；`extension` 由 name 后缀本地推导。
- FILETIME 转换公式：`datetime(1601-01-01, UTC) + ticks/10 µs`；实测
  `133295352000000000 → 2023-05-26T00:40:00Z`、`134331052108952560 → 2026-09-05T18:06:50.895256Z`。

### 1.4 搜索开关（布尔参数）

| 参数 | 行为 | 实测（同一 query 对照） |
|---|---|---|
| `regex=1` | query 按正则解释 | `search=blender.*\.exe&regex=1` → total=5；不加 flag（字面量）→ 0 |
| `case=1` | 区分大小写 | `Everything.exe`：case=1 → 6；case=0（默认）→ 7 |
| `wholeword=1` | 全字匹配 | `exe`：wholeword=1 → 33830；=0 → 44690 |
| `path=1` | **match-path 搜索开关**（搜索内容同时匹配完整路径），不是路径过滤器 | `blender`：path=1 → **22931**（272ms）；path=0/省略 → 620（40ms） |
| `matchpath=1` / `match_path=1` / `path=true` | ❌ 无效（参数名必须是 `path` 且真值为 `1`） | `blender` 均返回 620 |

> 注意：SPEC 早期把参数写成 `matchpath=1`，实际 Everything 1.4 接受的是 `path=1`
> （布尔真值）；本项目 `client` 的 `match_path` 参数映射为 HTTP `path=1`。

### 1.5 `path` / `filter` 不是过滤器

| 用法 | 实测 | 结论 |
|---|---|---|
| `search=blender&path=D:\software` | total=620（等于不过滤） | ❌ 不做路径过滤 |
| `search=*.exe&filter=D:\software` | total=26829（等于不过滤） | ❌ `filter` 参数被忽略 |
| `search=blender&path=1` | total=22931 | ✅ 仅作为 match-path 开关 |
| 正确做法 | `search=blender path:"D:\software"` | ✅ 用搜索语法限定路径 |

### 1.6 文件下载（能力覆盖）

- 文件链接形如 `http://127.0.0.1:8090/<URL-ENCODED-PATH>`（例如
  `http://127.0.0.1:8090/D%3A/software/blender-2.93.18-windows-x64/blender.exe`）
  （`/` + 盘符 URL 编码 `D%3A` + `/` 分隔路径；不需要 `?search=`）。
- 实测 `everything_mcp/client.py`（59294 字节）：
  `HTTP 200`、`Content-Type: text/x-python`、`Content-Length: 59294`、`Accept-Ranges: bytes`，
  **HTTP 返回字节的 sha256 与本地文件一致**。
- 不存在的路径 → `HTTP 404`（本项目 `everything_download` 据此给可操作错误）。
- 前提：`http_server_allow_file_download=1`（需开启）。**安全**：HTTP Server 无认证，
  只能绑定 `127.0.0.1`。

## 2. 搜索语法矩阵

> 下表 `total` 是实测值；环境为同一时刻索引（~3.20M 条），不同时刻会小幅变化。
> 命令统一为 `GET /?search=<URL编码>&json=1&count=5`（或 `count=1` 只看 total）。

### 2.1 已验证支持

| 语法 | 示例 | 实测 total / 备注 |
|---|---|---|
| 文件名子串 | `blender` | 620 |
| 通配符 `*` `?` | `blender*.exe` / `blender.ex?` / `*blender*` | 5 / 2 / 620 |
| `file:` / `folder:` | `file:blender` / `folder:blender` | 587 / 33（`folder:` 只匹配目录） |
| `ext:`（**分号分隔**） | `ext:exe;dll` / `ext:exe;dll;msi` | 130580 / 131072；大小写不敏感（`ext:EXE`=26827） |
| `size:`（见 §2.2） | `size:>100mb` / `size:<1kb` / `size:1mb..10mb` | 1111 / 34143 / 51960 |
| `dm:` 修改日期 | `dm:today` / `dm:last7days` / `dm:last24hours` / `dm:last30days` | 8722 / 174746 / 107871 / 210028 |
| `dm:` 绝对/区间 | `dm:>=2026-10-17` / `dm:2026-10-17..2026-10-18` / `dm:2026` / `dm:2026-10` | 108957 / 108957 / 1247575 / 181614 |
| `path:"..."` | `path:"D:\software"` | 68574（含子目录） |
| `parent:"..."`（**必须完整路径**） | `parent:"D:\software"` / `parent:"D:\software\blender-2.93.18-windows-x64"` | 26 / 33；`parent:blender`、`parent:software` → 0 |
| `nopath:` | `nopath:blender.exe` | 2（只按名称匹配，不匹配路径） |
| 内联 `regex:` | `regex:blender.*\.exe` | 5 |
| 内联 `case:` / `nocase:` | `case:Blender.exe` / `nocase:Blender.exe` | 0 / 2 |
| 内联 `wholeword:` | `wholeword:blender` | 174 |
| `wfn:`（完整文件名） | `wfn:blender.exe` / `wfn:*.exe` | 2 / 26829 |
| `startwith:` / `endwith:` | `startwith:blender` / `endwith:.exe` | 232 / 26829（参数大小写不敏感） |
| `len:`（文件名长度） | `len:>15` / `len:<5` | 1793670 / 173222 |
| `empty:`（空目录） | `empty:` / `empty: folder:` | 57923 / 57922 |
| 通配全匹配 | `*` | 3201114（等于索引总数） |
| OR `\|` | `blender\|everything.exe` / `blender \| everything.exe` | 627（620+7）/ 627 |
| NOT `!` | `blender !everything.exe` / `!ext:exe` | 620 / 3174390 |
| 引号短语 | `"Program Files"` / `"C:\Program Files"` | 8（精确词组）/ 554843（路径短语） |
| 中文 | `桌面` / `下载` / `截图` / `微信` / `项目` | 8 / 32 / 52 / 174 / 363 |
| 组合 | `dup.txt unique.txt`（AND）、`size:>0 path:"..."` | 0（无同时命中）/ 正常 |

### 2.2 `size:` 语义（**重要：带单位时是 KB 量化，不是字节精确**）

统一规则（2026-10-18 双人复核：临时文件对照 + 全盘计数）：
`size:N<unit>` 先把单位换算成 **KB 数** `U = N × unit_kb`（`kb=1`、`mb=1024`、`gb=1024²`），
再用 **`ceil(size/1024)`** 与 U 比较（等于 / 区间 / `<` / `<=` / `>` 同理）。
即带单位的 `size:` 实际按「向上取整的 KB 值」量化，`mb/gb` 也**不是**字节精确。
例外：无单位或 `b` 时按**精确字节**比较；`k`/`m`（不带 `b`）被忽略，等价于字节数 N。

| 写法 | 语义 | 实测 |
|---|---|---|
| `size:N` / `size:Nb` | **精确字节** | `size:1024`/`size:1024b` → 只命中 1024B；`size:0` → 只命中 0B；`size:1..1024` → 1~1024B |
| `size:Nkb` | `ceil(size/1024) == N`（窗口 `(N-1)×1024 < size ≤ N×1024`） | `size:1kb` → **1~1024B**；`size:<1kb` → **只命中 0 字节**；`size:<=1kb` → 0~1024B；`size:2kb` → 1025~2048B；`size:1kb..2kb` → 1~2048B |
| `size:Nmb` | `ceil(size/1024) == N×1024`（**宽度仍为 1024 字节的 KB 窗口**） | `size:1mb` 命中 `(1047552, 1048576]`；等价于 `size:1047553..1048576`（全盘复核均为 712 条）；**不等于** `size:1048576`（精确字节，632~680 条） |
| `size:Ngb` | `ceil(size/1024) == N×1024²` | `size:>1gb` 与 `size:>1073741824` 结果相同（96 条）：因为 1GB 恰好是 1024 的整数倍 |
| `size:Nk` / `size:Nm`（无 `b`） | `k`/`m` 不被识别 → 等价于字节数 N | `size:1k` 只命中 1B（与 `size:1` 相同，826 条）；`size:1024k` 只命中 1024B |
| `size:0kb` / `size:0mb` | 无结果 | 0（N=0 带单位无效） |
| `size:empty` | 0 字节文件 | 1（临时目录中 0B 文件） |
| `size:small` | ❌ 1.4 无结果 | 0 |

> **面向模型的建议**：需要精确字节窗口时用**无单位或 `b`**（`size:>104857600`、
> `size:1047553..1048576`）；用 `kb/mb/gb` 时必须在帮助里写明「按向上取整的 KB 量化」，
> 否则 `size:<1kb`、`size:1mb` 的结果会与直觉不符。SPEC §1.2 已记录该实测结论。

### 2.3 `dm:` 日期语法

| 语法 | 实测 total | 说明 |
|---|---|---|
| `dm:today` | 8832 | 今天修改 |
| `dm:last24hours` | 107871 | 最近 24 小时 |
| `dm:last7days` | 174746 | 最近 7 天 |
| `dm:last30days` / `dm:thisweek` / `dm:thismonth` | 210028 / 160782 / 181614 | |
| `dm:>=2026-10-17` / `dm:2026-10-17..2026-10-18` | 108957 / 108957 | 绝对日期/区间 |
| `dm:2026` / `dm:2026-10` | 1247575 / 181614 | 年 / 年月 |
| `dm:last1hour` | **0** | ❌ 1.4 不支持小时级相对语法；任意小时窗需自行算 `dm:>=ISO`/`dm:起..止` 并给 warnings |

### 2.4 排序是「自然排序」，不是码点序

`sort=name`（含默认排序）使用 Everything 的本地化自然排序：中文/符号/数字/字母的
先后与 Python 的 `sorted()` 不同（例如中文标题可能排在 `#` 之前，大小写/数字按自然序）。
**测试不要断言严格字典序**；本项目测试只断言「分页不重叠」「升序/降序首尾单调性（对
可比较字段）」「与默认排序是否一致（用于验证不支持项回退）」。

### 2.5 未知前缀 = 无结果（重要鉴别方法）

Everything 1.4 对**不认识的 `XYZ:` 前缀不会忽略**，而是当字面量/无结果：

| 查询 | total | 含义 |
|---|---|---|
| `abcxyz:blender.exe` | 0 | 未知前缀 → 0 |
| `zzz:blender.exe` | 0 | 未知前缀 → 0 |
| `dupe:blender` | 448 | ✅ `dupe:` 是**真实函数**（`blender`=620 的子集） |
| `dupe:`（空参数） | 2122360 | ✅ 全盘同名重复项（较慢） |
| `distinct:name` / `distinct:` | 0 / 0 | ❌ 1.5 特性 |
| `sort:size` / `sort:name` | 0 | ❌ 1.5 特性（sort 只能走 HTTP 参数） |
| `run_count:0` / `run_count:>0` | 0 / 0 | ❌ 1.4 无结果（`run_count`/`date_run` 属 1.5） |
| `date_run:today` / `date_run:` | 0 / 0 | ❌ 1.4 无结果 |

## 3. 不支持 / 慢查询 / 1.5 特性

### 3.1 慢查询（必须给 warnings，不要默认触发）

| 查询 | 现象 | 原因 |
|---|---|---|
| `dc:today` / `dc:>=2026-10-01`（创建时间） | **>5s 超时**（2s/5s 均超时） | `index_date_created=0`：未索引创建时间 → 全盘扫描 |
| `da:today`（访问时间） | **>5s 超时** | `index_date_accessed=0` |
| `attrib:AH` / `attrib:H`（属性） | **>5s 超时** | `index_attributes=0` |
| `dupe:` / `dupe:*.exe` | 180~240ms | 同名重复检测需全盘遍历（正常但不快） |
| `path=1`（match-path） | 40ms → **272ms**（`blender` 620→22931 条） | 需要匹配完整路径 |
| `regex=1` | 60~110ms（`blender.*\.exe`） | 正则匹配比子串慢 |

**可操作的提示文案**：若需要 `dc:`/`da:`/`attrib:`，需在 Everything 中启用
「索引日期创建/访问时间/属性」并重建索引（见 §4），否则请改用 `dm:`（修改时间）。

### 3.2 1.4 不支持（不要在帮助/预设里推荐）

- `distinct:`、`sort:`（搜索语法）、`run_count:`、`date_run:` —— 1.5 特性，1.4 返回 0。
- `dm:last1hour` 等**小时级相对语法**（分钟/小时）→ 0；改用绝对时间或 `dm:last24hours`。
- `ext:` 逗号分隔（`ext:exe,dll` → 0）、`ext:` 通配（`ext:py*` → 0）；必须分号精确扩展名。
- `parent:短名`（如 `parent:software`）→ 0；必须完整路径。
- `size:small` → 0。
- 结果列 `date_created/date_accessed/attributes/extension/run_count`：HTTP 参数静默忽略。

### 3.3 1.5 特性（升级后可重新评估）

| 1.5 特性 | 1.4.1 现状 |
|---|---|
| `dupe:` | **已支持**（实测），但较慢；本项目 `duplicates(mode="name")` 会优先使用 |
| `distinct:` | ❌ 返回 0 |
| `run_count:` / `date_run:` / 运行历史 | ❌ 返回 0（1.4 也有运行历史，但搜索语法不同/未启用） |
| `attrib:` 快速搜索 | 语法存在但未索引 → >5s；需 `index_attributes=1` |
| `dc:` / `da:` | 语法存在但未索引 → >5s；需 `index_date_created=1` / `index_date_accessed=1` |
| 列：date_created/date_accessed/attributes | HTTP 1.4 不支持（静默忽略） |

> 升级 1.5 后建议：重跑 `tests/test_mcp.py` 与本文 §2/§3 的关键命令，确认 dupe:/distinct:/
> 列参数行为是否变化；帮助矩阵中的 `version` 字段会显示实际版本。

## 4. 索引开关（Everything.ini）与启用建议

| ini 键 | 参考值 | 作用 | 开启代价/风险 |
|---|---|---|---|
| `index_size` | `1` | 索引大小（文件大小搜索/排序） | 已开；`fast_size_sort=1` |
| `index_date_modified` | `1` | 索引修改时间（`dm:`） | 已开；`fast_date_modified_sort=1` |
| `index_date_created` | `0` | 索引创建时间（`dc:`/date_created 列） | 打开后**需重建索引**（全盘，数十分钟）；DB 变大；`fast_date_created_sort=0` |
| `index_date_accessed` | `0` | 索引访问时间（`da:`） | 同上，且访问时间会频繁变更 → 常驻写盘/重建更频繁 |
| `index_attributes` | `0` | 索引文件属性（`attrib:`） | 同上；属性变化会触发索引更新 |
| `index_folder_size` | `0` | 索引文件夹大小 | 会显著增加索引成本；`size:` 对目录才有意义 |
| `fast_*_sort` | `fast_size_sort=1`、`fast_date_modified_sort=1`、其余 0 | 预排序加速 | 仅对已索引字段有意义 |

**建议**：

1. **默认不改 ini**。当前三项未索引字段的慢查询由工具层拦截并给 warnings（本项目 `help`/`status`
   会检测并提示），避免模型误触发 >5s 扫描。
2. 若业务确实需要按创建/访问时间或属性过滤，再按 NOTES 的流程**完全退出 Everything → 改 ini →
   重启**，并准备数十分钟的首次全盘索引时间；改完用 `everything_status` 核对
   `index.date_created_indexed` 等标志。
3. 任何 `dc:`/`da:`/`attrib:` 查询都必须设短超时（HTTP `EV_HTTP_TIMEOUT` 默认 2s 会先超时），
   工具层应把它转成「未索引，需启用」的可操作错误，而不是长时间卡住。

## 5. 实时索引行为（含环境怪事）

- **python/pwsh 创建/修改/删除**：Everything 通过 NTFS USN journal 实时更新，实测 1~5s 内可见；
  删除后同样 1~5s 内从索引消失（测试用轮询 `count==0` 判定）。
- 临测方法：在工作区临时目录用 `Path.write_bytes/text` 造文件 → 轮询 `path:"<dir>"` 的
  `totalResults` 直到等于预期 → 测完删除 → 再轮询归零。
- **⚠️ 环境怪事（仅影响开发期的验证手法）**：由 DSH `write` 工具新建/覆盖的文件，
  Everything 索引**看不到**，直到该文件被其它进程（pwsh/Python）再写一次。
  实测：`write` 工具建文件后 30s+ 搜索为 0；用 `edit` 工具改一次后立即出现；
  同批 pwsh 创建的文件立即出现。**测试造数据必须用 python/pwsh**，不要用 DSH write 后
  立刻断言索引可见。生产工具不依赖「索引能看见自己写的文件」，不受影响。

## 6. 对 v2 工具/文档的落地建议（已落实到 `everything_help` / README）

1. **`everything_help`**：`syntax_supported` 只列 §2.1；`syntax_unsupported` 列 §3.2；
   `slow_queries` 列 §3.1（`dc:`/`da:`/`attrib:`/`dupe:`/`path=1`/`regex`）；
   `http_params` 按 §1 写（只暴露 3 列、只推荐 4 个 sort、明确 `path=1` 是 match-path）。
2. **`everything_search`**：
   - `count_only=true` 用 `count=0`（只回 total，省 token）。
   - 未请求 `path/size/date_modified` 时不下发对应列参数。
   - `sort` 白名单 4 个；其它值进 `warnings` 并回退默认；`path_scope` 组合
     `path:"..."` + 客户端前缀二次过滤；`folder_only/file_only` 组合 `folder:/file:`。
   - 默认 `EV_MAX_RESULTS_CAP=1000` 是**产品上限**（HTTP 无上限，不要误以为索引限制）。
3. **`everything_duplicates(mode="name")`**：优先用 `dupe:`（1.4.1 支持，~200ms），
   `mode="name_size"` 用 dupe 候选再按大小分组；`dupe:` 不可用/报错时回退到分页扫描并给
   `warnings`（测试需覆盖回退路径）。
4. **`everything_changes`**：`dm:last1hour` 不支持 → `since` 一律换算成绝对时间
   `dm:>=<ISO>`（或 `dm:<起>..<止>`）；窗口 <24h 时附 warnings。
5. **`everything_grep`**：依赖 rg 探测（`EV_RG_EXE` → DSH 自带路径 → `where rg` → 候选）；
   rg 缺失时返回可操作错误（不得崩溃）；`--max-filesize`、超时、结果上限必须显式传。
6. **文档**：README 的工具表/示例与本文一致；NOTES 记录 1.4/1.5 差异、慢查询与索引开关、
   rg 依赖、回滚步骤。

## 7. 复现方式

本文所有结论都可以用「HTTP + 临时文件」复现；推荐直接用扩展后的测试套件（它把关键
矩阵固化成断言，且测试结束自动清理临时文件）：

```powershell
python <PATH-TO-REPO>\tests\test_mcp.py
```

单条命令模板（PowerShell，避免中文编码问题可改用 Python urllib）：

```powershell
# 索引总数 / count=0
(Invoke-RestMethod 'http://127.0.0.1:8090/?search=&json=1&count=0').totalResults

# 分页 + 排序
(Invoke-RestMethod 'http://127.0.0.1:8090/?search=ext%3Aexe&json=1&count=5&sort=size&ascending=0&size_column=1').results

# 列参数
(Invoke-RestMethod 'http://127.0.0.1:8090/?search=everything.exe&json=1&count=2&path_column=1&size_column=1&date_modified_column=1').results

# dupe:（较慢，~200ms）
(Invoke-RestMethod 'http://127.0.0.1:8090/?search=dupe%3Ablender&json=1&count=1').totalResults

# 慢查询（会超时/很慢，dc/da/attrib 不要默认触发）
(Invoke-RestMethod 'http://127.0.0.1:8090/?search=dc%3Atoday&json=1&count=1' -TimeoutSec 2)
```

## 附录 A：关键数量快照（2026-10-18，索引约 3,201,1xx 条）

| 指标 | 实测值 |
|---|---|
| HTTP 热查询（空查询/count=1） | 5.5ms |
| `everything.exe` 搜索（冷） | 44.3ms |
| `regex=blender.*\.exe`（热） | 60~110ms（5 条） |
| `path=1`（match-path，`blender`） | 272ms（22931 条） |
| `dupe:` 全盘 | 178~238ms（212 万条重复项） |
| `dc:` / `da:` / `attrib:` | >5s 超时（未索引；工具应拦截） |
| HTTP 文件下载（59294B） | 2.3ms，sha256 一致 |
| 实时索引：新建临时文件可见 | 1~5s（python/pwsh） |
| 索引总数 | 3,201,114 → 3,201,232（探测期间随索引更新小幅变化） |

## 附录 B：本矩阵的实测记录

- 原始探测脚本与 JSON 快照（`docs/_probe_*.py/json`、临时目录）已按仓库整洁要求清理；
  探测结论已固化到上表，并由 `tests/test_mcp.py` 的关键断言持续回归。
- 若需重新测量某个结论：用 §7 的单条命令模板；需要精确对照 `size:` 语义时，按
  「python 造已知大小文件 → `path:"<tmp>"` 限定范围 → 验证 → 删除」步骤进行。
