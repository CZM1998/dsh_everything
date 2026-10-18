# Development notes: system changes, verified behaviour, rollback

本文件记录 `everything-mcp` 依赖的**工作区外**改动、风险、回滚方式，以及开发期验证结论。
`everything-mcp/` 之外的文件本服务器运行时**不写**；下列改动均由用户手动执行一次。
能力事实的完整矩阵见 [docs/CAPABILITIES.md](docs/CAPABILITIES.md)。

## 一、改动清单（按依赖顺序）

### 1. 启用 Everything 内置 HTTP Server（一次性、手动）

文件：`%APPDATA%\Everything\Everything.ini`（最小配置片段见
[docs/everything.ini.example](docs/everything.ini.example)）

修改四项：

```ini
http_server_enabled=1
http_server_port=8090
http_server_bindings=127.0.0.1
http_server_allow_file_download=1
```

操作命令（**必须先完全退出 Everything**，否则退出时会把内存配置写回覆盖修改）：

```powershell
Get-Process Everything -ErrorAction SilentlyContinue | Stop-Process   # 或托盘右键退出
$ini = "$env:APPDATA\Everything\Everything.ini"
Copy-Item $ini "$ini.bak-$(Get-Date -Format yyyyMMddHHmmss)"
(Get-Content $ini) -replace '^http_server_enabled=.*', 'http_server_enabled=1' `
                    -replace '^http_server_port=.*', 'http_server_port=8090' `
                    -replace '^http_server_bindings=.*', 'http_server_bindings=127.0.0.1' `
                    -replace '^http_server_allow_file_download=.*', 'http_server_allow_file_download=1' |
  Set-Content $ini -Encoding ASCII
& 'C:\Program Files\Everything\Everything.exe' -startup
```

验证：

```powershell
(Invoke-WebRequest 'http://127.0.0.1:8090/?search=&json=1&count=1').Content
# 文件下载能力（应 200 + Content-Length；<URL-ENCODED-PATH> 换成任一已索引文件的 URL 编码路径）
(Invoke-WebRequest 'http://127.0.0.1:8090/<URL-ENCODED-PATH>').StatusCode
```

**风险**：

- Everything 的 HTTP 接口**没有认证**。`http_server_bindings` 必须只写 `127.0.0.1`；
  写成 `0.0.0.0`/机器 IP 会把整盘文件列表、文件下载暴露到局域网。**不要**写
  `127.0.0.1:8090`（含端口），Everything 会静默不启动 HTTP Server。
- `http_server_allow_file_download=1` 是 `everything_download` 的前提；开启后本机 loopback
  可通过 HTTP 读取任意已索引文件（这就是该工具的设计用途）。
- 修改 ini 时若 Everything 仍在运行，退出时配置会被覆盖回旧值 → 必须先退出再改、再启动。
- 端口 8090 若被其他程序占用，Everything 不会启动 HTTP；用 `netstat -ano | findstr :8090`
  核对，必要时同时改 `http_server_port` 与本项目 `EV_HTTP_PORT`。

**回滚**：把四项改回 `http_server_enabled=0`、`http_server_port=80`、
`http_server_bindings=`、`http_server_allow_file_download=0`（或恢复上面备份的 ini），
重启 Everything。注意：关闭 HTTP 后本 MCP 服务器不可用（工具会返回可操作错误）。

### 2. 默认 Everything 实例常驻（用户已同意/现状）

服务器默认复用当前用户的**默认实例**（`-startup` 启动，监听
`127.0.0.1:8090`）。它由用户登录自启或手动启动，索引常驻内存（约 700MB~1GB），
保证查询毫秒级、索引实时。

`EV_AUTO_START=1`（默认）时，若 HTTP 不可达且探测不到 Everything 进程，MCP 服务器会执行：

```text
C:\Program Files\Everything\Everything.exe -startup
```

最小化、不抢焦点，子进程 stdio 全部指向 NUL，不占用 MCP 的 stdout 管道。

**风险**：会多一个最小化的 Everything 窗口/托盘图标；若用户不想自动启动，设 `EV_AUTO_START=0`，
由用户自行保证 Everything 在运行。

**回滚**：`EV_AUTO_START=0`，并手动结束 Everything。

### 3. 注册到 DSH profile + 重载（一次性、手动）

在目标 profile 的 patch 中加入 `@deepseek-ai/dsh-mcp-client` 条目（完整 YAML 见
[README.md](README.md)），改完需重启 `dsh web` 使新 profile 生效。
v2 建议补充的 env（都可选，缺省自动探测/默认值）：

- `EV_RG_EXE`：ripgrep 路径（不配也能自动找到 DSH 自带路径）。
- `EV_DOWNLOAD_ROOT` / `EV_EXPORT_ROOT`：写盘允许根（默认 workspace）。
- `EV_PRESETS_FILE`：自定义预设文件（可选）。
- `EV_ALLOW_SIDE_EFFECTS=0`：想完全禁用 `everything_reveal` 时设置。
- `EV_SCAN_CAP` / `EV_HASH_MAX_BYTES`：聚合/哈希上限（默认 50000 / 256MB）。

**热重载（不改 profile）**：结束当前 MCP server 的 python 子进程，DSH 会在下次调用时
自动重连并加载新代码：

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -like '*everything-mcp*server.py*' -and $_.Name -like 'python*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
# 然后调用 mcp__everything__everything_help 验证
```

**风险**：

- profile patch 语法错误会导致 harness 启动异常；建议先备份 `cordis.patch.yml`（备份文件不要提交）。
- `failOnStartupError` 保持默认 `false`：即使 MCP 连接失败，harness 仍可启动，只是工具不出现。
- 工具名固定为 `mcp__everything__*`；`serverName` 必须与 profile 中一致（本项目实测用 `everything`）。
- **不要**结束 Everything.exe；只需结束 MCP 的 python 子进程。

**回滚**：删除该 MCP 条目并重启 `dsh web`；或仅回退新增 env（它们都有默认值，删除不影响启动）。

## 二、验证过的结论与踩坑（避免重复劳动）

### A. v1 结论（保留）

1. **启动参数是 `-config` 不是 `-ini`**。Everything 1.4 传 `-ini` 会弹“命令行选项”对话框并退出。
2. **命名实例不可用**：`-instance <name>` / 在自定义 ini 里设 `instance_name` 的实例**无法使用
   “Everything” 服务**，启动时弹“需要管理员权限来索引 NTFS 卷”对话框，索引为空（实测
   `totalResults=0`）。因此生产**必须复用默认实例**，不要用 `-instance`/自定义 ini 隔离。
3. **`http_server_bindings` 只写 IP**；写 `127.0.0.1:8090` 会导致 HTTP Server 静默不启动。
4. **自定义 `-config` + 用户 DB**：默认实例下可加载用户 `%LOCALAPPDATA%\Everything\Everything.db`
   （135MB）；命名实例则因服务不可用而空索引。本项目不复制/修改用户 DB。
5. **`-config` 指定自定义配置时**，Everything 仍可能把部分设置写回 `%APPDATA%`；本项目默认
   不传 `-config`，`EV_EVERYTHING_CONFIG` 仅作高级逃生口。
6. **HTTP JSON 契约**：`{"totalResults":N,"results":[{"type":"file","name":...,"path":"C:\\...\\",
   "size":"2265096","date_modified":"133295352000000000"}]}`——`size`/日期是**字符串**，日期是
   Windows FILETIME（100ns），本服务器已转换为整数与 ISO 8601 UTC。
7. **HTTP 参数**：`search/json/count/offset/regex/case/wholeword/path/sort/ascending/
   path_column/size_column/date_modified_column/...`；`path=1` 表示同时匹配路径。
8. **DSH 沙箱限制**：workspace-write 下官方 MCP SDK 的 asyncio 子进程使用“重叠命名管道”，
   会 `PermissionError: [WinError 5]`；`tests/test_mcp.py` 因此实现了三级传输自适应
   （官方 stdio → 匿名管道 JSON-RPC → 内存传输）。生产 DSH 前台环境正常使用官方 stdio。
9. **空索引判断**：`everything_status.index.total_items == 0` 且 HTTP 200 时，通常是索引仍在
   构建/回放 USN journal，稍后重试即可；本项目测试对 Tier B 采用 240s 轮询 + 超时 SKIP。

### B. v2 实测结论（2026-10-18 独立验证 + 联调）

10. **`dupe:` 在 1.4.1.1024 可用但慢**：`dupe:` 空查询全盘约 2,122,360 条、`dupe:blender`
    448 条（普通 `blender` 620），耗时约 180~240ms（比普通查询慢两个数量级）。未知前缀
    控制组（`abcxyz:blender.exe`/`zzz:blender.exe`）返回 0，证明 `dupe:` 是真实函数而非
    “未知前缀被忽略”。`everything_duplicates(mode=name/name_size)` 会优先用 `dupe:`，
    探测结果按 client 缓存 120s；探测失败自动回退分页扫描并给 warnings。
11. **`size:` 带单位时是 KB 量化**：`kb/mb/gb` 先换算成 KB 数 U（`mb=1024`、`gb=1024²`），
    再按 `ceil(size/1024) == U` 比较；`size:1kb` 命中 1~1024B、`size:<1kb` 只命中 0B、
    `size:1mb` 等价于 `size:1047553..1048576`（不等于精确字节 `size:1048576`）；
    无单位或 `b` 才是精确字节；`k`/`m`（无 b）被忽略等价于字节数。帮助/README 必须按此措辞。
12. **`count` 无上限**：HTTP `count=1000/2000/5000/10000/20000` 均按请求返回；
    `count=0` 只回 total；负数/非法 → 0 条。`EV_MAX_RESULTS_CAP=1000` 是本项目**主动**
    的 token/带宽上限，不是 Everything 限制。
13. **`path=1` 是 match-path 搜索开关**（不是路径过滤）：`blender` 620 → 22931（272ms）；
    `path=<路径>`、`filter=` 作为过滤参数无效；路径范围必须用 `path:"..."`/`parent:"..."`；
    `matchpath=1`/`match_path=1` 参数名无效。`parent:` 必须完整路径。
14. **`dm:last1hour` 等小时级相对语法在 1.4 返回 0**；`everything_changes` 已统一换算成
    绝对 `dm:>=YYYY-MM-DD` + 客户端精确时间窗，并给 warnings（不要依赖 `last1hour`）。
15. **`distinct:`/`sort:`（搜索语法）/`run_count:`/`date_run:` 是 1.5 特性**，1.4 各种写法
    均返回 0（`attrib:`/`dc:`/`da:` 语法存在但未索引 → >5s，见 §三）。
16. **HTTP 文件下载可用**：`GET /D%3A/...` 返回 200 + Content-Length/Content-Type，sha256 与
    本地一致；不存在 → 404；`http_server_allow_file_download=1` 是前提。
17. **实时索引正常**：python/pwsh 创建/修改/删除文件后 1~5s 内被 Everything 索引/移除
    （测试用轮询 `count==0` 验证回收）。
18. **环境怪事——DSH `write` 工具创建的文件 Everything 索引看不到**：write 工具新建/覆盖
    文件后 30s+ 搜索仍为 0；被其它进程（pwsh/python）再写一次或 `edit` 工具修改后立即可见。
    因此**测试造数据必须用 python/pwsh**；这仅影响“用 Everything 验证文件是否存在”的
    开发手法，不影响工具运行（工具读文件系统，不依赖索引看见自己写的文件）。
19. **结果列只有 3 个**：`path_column/size_column/date_modified_column` 生效；
    `date_created/date_accessed/attributes/extension/run_count_column` 静默忽略。
20. **排序只有 4 个**：`name/path/size/date_modified` 生效；其余静默回退默认 name 升序；
    `ascending` 省略时实测为升序；name 是 Everything 自然排序（非码点序）。
21. **`ext:` 只认分号**（`ext:exe,dll`、`ext:py*` 均 0）；`size:small` 0；`empty:` 匹配空目录。
22. **性能（v2 全量自测）**：搜索热 P95 3.7~24.5ms（稳态 3.1~5.0ms 中位）、count 平均
    10.0~18.6ms、summary（17 文件）38.1~55.1ms、duplicates(name，dupe: 路径)
    163.0~189.3ms、grep（真实 rg）115.6~138.8ms；v1 基线：正则平均 26.5~31.3ms、
    递归扫描 D:\software 873.5~1161.3ms（约 29~39x）。
23. **`everything_duplicates` 默认输出受控**：`max_groups=10`、`max_paths_per_group=2`、
    `max_response_bytes=4096`（超预算自动压缩，`payload_bytes/payload_truncated` 标记）；
    `D:\software` 下 `name_size` 默认响应实测 3.6~4.1KB < 4KB，`paths` 只回前 2 条、
    完整数量看 `path_count`。`everything_summary` 默认 `include_folders=false`（只聚合文件，
    size 才有意义），`include_folders=true` 时 `aggregates="files+folders"`。

## 三、慢查询与索引开关（Everything.ini）

| ini 键 | 参考值 | 作用 | 开启代价/风险 |
|---|---|---|---|
| `index_size` | `1` | 索引大小（size 搜索/排序） | 已开；`fast_size_sort=1` |
| `index_date_modified` | `1` | 索引修改时间（`dm:`） | 已开；`fast_date_modified_sort=1` |
| `index_date_created` | `0` | 索引创建时间（`dc:` / date_created 列） | 打开后**需重建索引**（全盘，数十分钟）；DB 变大 |
| `index_date_accessed` | `0` | 索引访问时间（`da:`） | 同上；访问时间频繁变更 → 写盘/重建更频繁 |
| `index_attributes` | `0` | 索引文件属性（`attrib:`） | 同上；属性变化触发索引更新 |
| `index_folder_size` | `0` | 索引文件夹大小 | 显著增加索引成本 |

- `dc:`/`da:`/`attrib:` 未建索引时是**全盘扫描，实测 >5s 超时**（HTTP 默认 2s 会先超时）。
  工具层已做提示（`everything_help` 的 `slow_queries`、`everything_status.hints`），
  禁止在帮助/预设里推荐这些语法。
- 若确需启用：**完全退出 Everything → 改 ini 对应项为 1 → 重启并等待首次全盘索引完成**；
  改完用 `everything_status.index_switches` 核对。启用后索引 DB 会变大，机器磁盘/内存
  占用上升，属于系统级改动，需要管理员权限并在低峰期做。
- **建议默认不改 ini**：慢查询由工具层拦截/提示即可；`dm:`（修改时间）已足够覆盖大多数
  “最近变更”场景。

## 四、ripgrep（ripgrep）依赖

- `everything_grep` 依赖 `rg.exe`；探测顺序：`EV_RG_EXE` → DSH 自带
  `%APPDATA%\npm\node_modules\@deepseek-ai\dsh\node_modules\@vscode\ripgrep-win32-x64\bin\rg.exe`
  → `where rg` → 已知候选（VS Code/Cursor/scoop/cargo 等）。
- 找不到时返回可操作错误（安装 `winget install BurntSushi.ripgrep.MSVC` / 设置 `EV_RG_EXE`），
  不会崩溃，其他工具不受影响。
- 本项目**不下载/不安装**任何包；rg 由用户或 DSH 自带提供。
- 测试中的“rg 缺失”路径通过内存传输 + monkeypatch `rg.find_rg` 模拟，验证错误信息。

## 五、明确不做 / 不建议

- 不修改 `C:\Program Files\Everything\` 下任何文件（安装目录的 `Everything.ini` 属于安装配置）。
- 不写 `%APPDATA%`/`%LOCALAPPDATA%`；不复制、不重建用户 Everything 数据库。
- 不下载/不引入 `es.exe`、Everything SDK、任何 npm/pip 包。
- 不实现 IPC（WM_COPYDATA）查询协议与 ETP：Everything 1.4 的 IPC 查询回复需要 SDK 的共享内存
  协议，环境中无 SDK 且本仓库不下载依赖；HTTP + 自动拉起已满足毫秒级搜索需求。IPC 窗口探测仅用于
  “是否在运行”的诊断。
- 不 kill 用户的 Everything 进程、不占用/切换默认实例。
- 不使用 `-instance`/自定义配置隔离实例（会空索引）。

## 六、回滚

| 改动 | 回滚方式 |
|---|---|
| Everything HTTP Server 四项 ini | 改回 `http_server_enabled=0`、`http_server_port=80`、`http_server_bindings=`、`http_server_allow_file_download=0`，或恢复 `Everything.ini.bak-*`，重启 Everything |
| MCP profile 条目 / 新增 EV_* env | 删除 MCP 条目并重启 `dsh web`；新增 env 都有默认值，直接删除即可回到 v1 行为 |
| MCP 代码（v2 → v1） | 结束 MCP python 子进程后，用 git 历史/备份恢复改动文件；v2 新增文件为 `everything_mcp/{presets,help,compose_tools,rg}.py`，删除它们后 server.py 会按“组合工具缺失降级”方式启动（但查询层仍为 v2，需要同时恢复 server.py/client.py/http_channel.py/config.py） |
| 仅禁用组合工具 | 让 `everything_mcp/compose_tools.py` 不可导入（重命名/删除），`register_compose_tools()` 会告警并跳过，查询类 7 个工具仍可用 |
| 禁用 reveal | `EV_ALLOW_SIDE_EFFECTS=0`（每次调用重新读取，无需重启） |
| 禁止导出/下载写盘 | 把 `EV_EXPORT_ROOT`/`EV_DOWNLOAD_ROOT` 指向空目录或不可写目录（工具会返回错误，不影响只读工具） |
| 自动拉起 | `EV_AUTO_START=0` |

> 代码回滚前请先备份当前工作目录（尤其是 `everything_mcp/` 与 `tests/`）。

## 七、遗留事项

- `dev/` 是本地开发/排查目录，已被 `.gitignore` 排除；其中的 Everything `.ini`、备份与临时日志
  不会进入发布物，发布前可整体删除（`tests/test_mcp.py` 是官方测试入口）。
- 若后续升级 Everything 到 1.5：HTTP 参数与 JSON 字段可能新增（如 `path_column` 行为）、
  `dupe:/distinct:/run_count:/date_run:` 等搜索函数可能变化；建议升级后重跑
  `tests/test_mcp.py` 与 [docs/CAPABILITIES.md](docs/CAPABILITIES.md) §2/§3 的关键命令，
  并更新帮助矩阵中的 `verified_everything_version`。
- 端口变更需同时改 Everything ini 与 profile 里的 `EV_HTTP_PORT`（或环境变量），两者不一致时
  `everything_status` 会给出明确提示。
- `everything_duplicates` 的真实目录（如 `D:\software`）输出已默认受控（`max_groups=10`、
  `max_paths_per_group=2`、`max_response_bytes=4096`）；仍过大时可进一步调小，或加
  `min_size`/缩小 `path_scope`。`everything_summary` 大范围扫描可用 `max_scan` 限制
  （受 `EV_SCAN_CAP` 限制），默认只聚合文件（`include_folders=false`）。
