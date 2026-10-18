"""Everything 能力矩阵 + 工具清单 + 示例（``everything_help`` 与 MCP 资源）。

本模块是“全部能力”对模型的自解释入口，内容严格来自 SPEC §1 的已核实事实
（Everything 1.4.1.1024 + 内置 HTTP Server，2026-10-18 实测，含 task-5 补充语义）：

- **支持**：1.4 可用搜索语法、HTTP 参数、列、排序；`dupe:`（同名重复）在
  1.4.1 实测可用，但全盘约 180–240ms，列为“支持但慢”；
- **不支持**：1.5 特性（`distinct:`/`sort:`/`run_count:`/`date_run:`/
  `attrib:`）、1.4 HTTP 不返回的列、被忽略的参数（`path=<路径>`/`filter`）、
  小时级相对时间（`dm:last1hour` 等）；
- **慢查询**：`dupe:`（可用但慢）；`dc:`（创建时间）/`da:`（访问时间）/
  `attrib:` —— 因为 Everything.ini 中 `index_date_created=0`、
  `index_date_accessed=0`、`index_attributes=0`，需要改 ini + 重建索引才可用。

输出同时提供结构化 dict 与 Markdown 文本，供不同调用方使用。
"""

from __future__ import annotations

from . import __version__
from .http_channel import SUPPORTED_COLUMN_KEYS, SUPPORTED_SORT_KEYS

#: 本服务器验证过的 Everything 版本（能力矩阵的事实来源）
VERIFIED_EVERYTHING_VERSION = "1.4.1.1024"

# ----------------------------------------------------------------------
# 搜索语法矩阵（1.4 实测）
# ----------------------------------------------------------------------
SYNTAX_SUPPORTED: dict[str, list[str]] = {
    "文件名/通配符": ["*.exe", "report_2025*", "file?.log", '"精确 词组"'],
    "类型过滤": ["file:", "folder:"],
    "重复文件": [
        "dupe:（同名重复，**1.4.1 实测支持**；全盘 180–240ms，比普通查询慢两个数量级，适合 duplicates 场景）",
        "dupe:blender（同名且包含 blender 的文件）",
    ],
    "扩展名": [
        "ext:exe",
        "ext:exe;dll（只认分号）",
        "ext:tar.gz",
    ],
    "大小": [
        "size:>100mb",
        "size:<1kb（仅 0 字节文件）",
        "size:1024（字节精确，仅 1024B）",
        "size:1024..2048（区间，含端点）",
        "size:1mb（按 KB 档向上取整：命中 1047553..1048576B，非精确 1MB）",
    ],
    "修改时间": [
        "dm:today",
        "dm:yesterday",
        "dm:last7days",
        "dm:>=2026-10-17",
        "dm:2026-10-17..2026-10-18",
    ],
    "路径": [
        'path:"D:\\software"（包含匹配，可配通配符）',
        'parent:"D:\\software"（必须是完整路径，仅直接子项）',
        "nopath:foo（排除路径匹配）",
    ],
    "文本/长度": ["wfn:*.exe", "startwith:report", "endwith:.log", "len:>10"],
    "目录": ["empty:（空目录）"],
    "修饰符（写在 query 里）": [
        "regex:abc.*",
        "case:Foo（区分大小写）",
        "wholeword:foo（全字匹配）",
    ],
    "逻辑": ["a | b（OR）", "!a（NOT）", "空格表示 AND"],
}

#: 1.5 特性 / 1.4 不可用或极慢的语法（禁止在帮助与预设里推荐）
SYNTAX_UNSUPPORTED: dict[str, list[str]] = {
    "1.5 才有的查询函数（1.4 无结果）": [
        "distinct:",
        "sort:",
        "run_count:",
        "date_run:",
        "attrib:",
    ],
    "小时级相对时间（1.4 返回 0）": [
        "dm:last1hour / dm:last24hours 等小时级语法不支持",
        "需要任意时间窗时用 dm:>=<ISO 日期> 或 dm:<起>..<止> 自行计算后传入",
    ],
    "需要改 ini + 重建索引（默认未索引，查询极慢）": [
        "dc:（创建时间，index_date_created=0）",
        "da:（访问时间，index_date_accessed=0）",
        "attrib:（属性，index_attributes=0）",
    ],
    "语义限制": [
        'parent:名字 —— 必须是完整路径，如 parent:"D:\\software"',
        "ext: 只认分号分隔（ext:a;b）；逗号与通配（ext:py*）无效",
        "未知 XYZ: 前缀会被当字面量（不会报错，容易误判）",
    ],
    "命名实例（-instance/-config）": [
        "无法使用 Everything 服务 → 空索引 + 管理员弹窗；生产只复用默认实例",
    ],
}

#: HTTP JSON API 参数矩阵
HTTP_PARAMS: dict = {
    "supported": {
        "search": "查询字符串（Everything 语法/正则），必须 URL 编码",
        "json=1": "返回 JSON：{totalResults, results:[...]}",
        "count": (
            "本次返回条数，**无硬上限**（实测 1000/2000/5000/10000 均按请求返回）；"
            "count=0 只返回 totalResults；负数/非法值 → 0 条"
        ),
        "offset": "分页起点（0 基），与 count 搭配分页",
        "sort": (
            "仅 name | path | size | date_modified 生效；其余值静默回退默认 "
            "name 升序（注意 name 为 Everything 自然排序，非码点序）"
        ),
        "ascending": "0=降序，1=升序；缺省为升序",
        "path_column=1": "结果增加 path 列",
        "size_column=1": "结果增加 size 列（字符串，字节；本服务器转 int）",
        "date_modified_column=1": "结果增加 date_modified 列（FILETIME 100ns；本服务器转 ISO 8601 UTC）",
        "regex=1": "query 按正则解释",
        "case=1": "区分大小写",
        "wholeword=1": "全字匹配",
        "path=1（仅布尔真值 1 有效）": (
            "**match-path 搜索开关**：让查询同时匹配完整路径；"
            "如 search=blender&path=1 → 620→22931。它**不是**路径过滤"
        ),
    },
    "ignored": {
        "matchpath=1": (
            "**无效**：Everything 1.4 HTTP 只认 path=1；matchpath=1 对结果无影响"
            "（实测 search=blender&matchpath=1 仍为 620）；path=true / path=0 同样无效"
        ),
        "path=<路径>": "无效（path 只认布尔真值 1 = match-path 开关），路径范围请用搜索语法 path:\"...\"",
        "filter": "无效，不会过滤结果",
        "date_created_column": "1.4 HTTP 不支持，静默忽略",
        "date_accessed_column": "1.4 HTTP 不支持，静默忽略",
        "attributes_column": "1.4 HTTP 不支持，静默忽略",
        "extension_column": "1.4 HTTP 不支持，静默忽略",
        "run_count_column": "1.4 HTTP 不支持，静默忽略",
    },
    "columns_supported": list(SUPPORTED_COLUMN_KEYS),
    "columns_unsupported": ["date_created", "date_accessed", "attributes", "run_count"],
    "sort_supported": list(SUPPORTED_SORT_KEYS),
    "notes": [
        "结果始终包含 type（file/folder）与 name；path/size/date_modified 由列参数控制",
        (
            "size 语义（1.4.1 实测）：size:Nkb = ceil(size/1024)==N，如 size:1kb 命中 1..1024B、"
            "size:2kb 命中 1025..2048B；size:Nmb / size:Ngb 先换算成 KB 再向上取整，"
            "如 size:1mb 命中 1047553..1048576B（不是精确 1MB）；size:N / size:Nb 是字节精确；"
            "size:Nk / size:Nm（无 b）的 k/m 被忽略 = N 字节；size:0kb=0 条。"
            "精确比较优先用无单位字节或 range（size:1024..2048，含端点）"
        ),
        "count 无上限，但本服务器仍按 EV_MAX_RESULTS_CAP（默认 1000）限制单次返回，防止把整盘塞回模型",
        "文件下载：GET http://127.0.0.1:8090/C%3A/path/to/file（需 allow_file_download=1；HEAD 不支持）",
        "HTTP 接口无认证，必须只绑定 127.0.0.1",
    ],
}

#: 极慢/需要改配置的查询（status 与 help 都会提示）
SLOW_QUERIES: list[dict] = [
    {
        "syntax": "dupe:（同名重复）",
        "reason": (
            "1.4.1 实测支持，但全盘约 180–240ms，比普通查询（1~5ms）慢两个数量级；"
            "dupe:blender 约 448 条、dupe: 空查询全盘约 2,122,360 条"
        ),
        "fix": "适合 everything_duplicates 场景；不要在大范围轮询/循环里反复调用",
    },
    {
        "syntax": "dc:（创建时间）",
        "reason": "Everything.ini 中 index_date_created=0，未索引创建时间，回退到未索引搜索，极慢（>5s）",
        "fix": "启用需：完全退出 Everything → 改 Everything.ini index_date_created=1 → 重启并重建索引",
    },
    {
        "syntax": "da:（访问时间）",
        "reason": "index_date_accessed=0，未索引访问时间，极慢（>5s）",
        "fix": "同上，改 index_date_accessed=1 并重建索引",
    },
    {
        "syntax": "attrib:（属性）",
        "reason": "index_attributes=0，未索引文件属性，极慢（>5s）",
        "fix": "同上，改 index_attributes=1 并重建索引",
    },
]

#: MCP 工具清单（core=本服务器查询层，compose=组合工具/条件注册）
TOOLS: list[dict] = [
    {
        "name": "everything_search",
        "category": "core",
        "purpose": "Everything 索引搜索文件/目录（毫秒级），支持分页/排序/字段裁剪",
        "key_params": [
            "query",
            "regex",
            "path_scope",
            "folder_only/file_only",
            "sort",
            "ascending",
            "offset",
            "max_results",
            "fields",
            "count_only",
        ],
    },
    {
        "name": "everything_count",
        "category": "core",
        "purpose": "只返回命中总数，最省 token 的存在性判断",
        "key_params": ["query", "regex", "path_scope", "folder_only/file_only"],
    },
    {
        "name": "everything_status",
        "category": "core",
        "purpose": "Everything/HTTP/索引/DB/卷/ini 索引开关/rg/预设/MCP 能力矩阵诊断",
        "key_params": [],
    },
    {
        "name": "everything_help",
        "category": "core",
        "purpose": "本帮助：支持/不支持/慢查询语法、HTTP 参数、工具与示例",
        "key_params": [],
    },
    {
        "name": "everything_presets",
        "category": "core",
        "purpose": "列出或运行内置/自定义预设查询",
        "key_params": ["action(list|run)", "name", "max_results", "offset", "path_scope"],
    },
    {
        "name": "everything_export",
        "category": "core",
        "purpose": "把命中结果导出为 csv/jsonl/efu（仅写 EV_EXPORT_ROOT 内）",
        "key_params": ["query", "out_csv", "format", "columns", "offset", "sort", "max_results"],
    },
    {
        "name": "everything_download",
        "category": "core",
        "purpose": "经 Everything HTTP Server 下载文件到 EV_DOWNLOAD_ROOT（大小上限/越权校验）",
        "key_params": ["path", "dest", "max_bytes"],
    },
    {
        "name": "everything_summary",
        "category": "compose",
        "purpose": "聚合统计（按扩展名/目录/类型/大小桶/月份分组）",
        "key_params": ["query", "group_by", "top_n", "max_scan"],
    },
    {
        "name": "everything_duplicates",
        "category": "compose",
        "purpose": "重复文件检测（name/name_size/size/hash，只读）",
        "key_params": ["mode", "query", "min_size", "max_groups", "hash_algo"],
    },
    {
        "name": "everything_grep",
        "category": "compose",
        "purpose": "用 ripgrep 在候选文件/目录中做内容搜索",
        "key_params": ["pattern", "fixed_strings", "query", "path_scope", "exts", "globs"],
    },
    {
        "name": "everything_batch_stat",
        "category": "compose",
        "purpose": "批量查询路径的 stat（存在/大小/时间/可选哈希），只读",
        "key_params": ["paths", "include_hash", "hash_algo"],
    },
    {
        "name": "everything_changes",
        "category": "compose",
        "purpose": "查询某时间窗口内的变更文件（24h/7d/today/ISO）",
        "key_params": ["since", "path_scope", "exts", "max_results"],
    },
    {
        "name": "everything_reveal",
        "category": "compose",
        "purpose": "在资源管理器中定位/打开路径（EV_ALLOW_SIDE_EFFECTS=0 时拒绝）",
        "key_params": ["path"],
    },
]

EXAMPLES: list[dict] = [
    {
        "intent": "找 D:\\software 下 100MB 以上的 exe",
        "tool": "everything_search",
        "arguments": {
            "query": 'path:"D:\\software" size:>100mb ext:exe',
            "sort": "size",
            "ascending": False,
            "fields": ["name", "path", "size"],
        },
    },
    {
        "intent": "最近 7 天改过的截图",
        "tool": "everything_presets",
        "arguments": {"action": "run", "name": "screenshots_7d", "max_results": 50},
    },
    {
        "intent": "分页浏览全部 .log 文件（每页 100 条）",
        "tool": "everything_search",
        "arguments": {"query": "ext:log", "sort": "date_modified", "max_results": 100, "offset": 0},
        "note": "下一页用返回的 next_offset 作为 offset；truncated=false 表示已到末页",
    },
    {
        "intent": "只判断是否存在某文件（最省 token）",
        "tool": "everything_count",
        "arguments": {"query": 'wfn:"important.docx"'},
    },
    {
        "intent": "导出 CSV 给 Excel",
        "tool": "everything_export",
        "arguments": {
            "query": 'ext:log size:>10mb',
            "out_csv": "reports/big_logs.csv",
            "format": "csv",
            "max_results": 5000,
        },
    },
    {
        "intent": "下载搜索到的文件到工作区",
        "tool": "everything_download",
        "arguments": {"path": "D:\\software\\blender-2.93.18-windows-x64\\blender.exe"},
    },
    {
        "intent": "找同名重复文件（1.4.1 可用但较慢，约 180–240ms；建议用 duplicates 工具）",
        "tool": "everything_duplicates",
        "arguments": {"mode": "name_size", "min_size": 1048576},
    },
    {
        "intent": "直接用 dupe: 查同名文件（1.4 支持但慢两个数量级）",
        "tool": "everything_search",
        "arguments": {"query": "dupe:blender", "max_results": 50},
    },
    {
        "intent": "在代码库中搜索 TODO（内容搜索，需组合工具）",
        "tool": "everything_grep",
        "arguments": {"pattern": "TODO", "path_scope": "D:\\repo", "exts": [".py", ".ts"]},
    },
    {
        "intent": "统计某个目录下文件类型分布（需组合工具）",
        "tool": "everything_summary",
        "arguments": {"query": "file:", "path_scope": "D:\\software", "group_by": "ext"},
    },
]

TIPS: list[str] = [
    "优先用 Everything 原生语法（ext:/path:/size:/dm:），它比正则快且语义清晰；regex=True 时不要与 ext: 等函数混用。",
    "size 是“按 KB 档向上取整”比较（1.4.1 实测）：size:1kb 命中 1..1024B，size:2kb 命中 1025..2048B；size:1mb 命中 1047553..1048576B（不是精确 1MB），size:1gb 同理；size:<1kb 仅命中 0 字节文件。要字节精确请用无单位值（size:1024）或 range（size:1024..2048，含端点）；size:1k / size:1m（无 b）的 k/m 被忽略 = 1 字节。",
    "ext: 只认分号分隔（ext:a;b）；逗号（ext:a,b）和通配（ext:py*）无效。",
    "path: 是“路径包含”匹配；parent: 必须是完整路径。两者都可用引号包裹含空格的路径。",
    "HTTP 参数 path=1 是 match-path 搜索开关（不是路径过滤）；路径范围请用搜索语法 path:\"...\"，filter= 参数无效。",
    "sort 只支持 name/path/size/date_modified，其余值会回退 name 升序；name 是 Everything 自然排序（非码点序），不要断言严格字典序。",
    "dupe:（同名重复）在 1.4.1 可用，但全盘约 180–240ms、比普通查询慢两个数量级；适合 duplicates 场景，不要循环频繁调用。",
    "dm:last1hour 等小时级相对语法 1.4 不支持（返回 0）；任意时间窗请用 dm:>=2026-10-17 或 dm:起..止 计算后传入。",
    "distinct:/sort:/run_count:/date_run:/attrib: 是 Everything 1.5 特性，1.4 不生效，本服务器不推荐。",
    "dc:/da:/attrib: 需要改 Everything.ini 索引开关并重建索引；否则极慢（>5s），status 会提示。",
    "只请求需要的字段（fields）可以显著减少 token；只判断存在性用 everything_count/count_only。",
    "结果有 truncated=true 时用 offset/next_offset 翻页，或 everything_export 导出全量（上限 10000 行）。",
    "大范围聚合前先估算命中数（everything_count），组合工具受 EV_SCAN_CAP 限制。",
    "中文路径/文件名可直接输入，HTTP 链路为 UTF-8。",
    "Everything HTTP 接口没有认证，只能绑定 127.0.0.1（本项目已如此配置）。",
]

#: MCP 资源 URI
RESOURCE_URIS: dict[str, str] = {
    "everything://help/syntax": "Everything 搜索语法能力矩阵（Markdown）",
    "everything://status": "Everything/HTTP/索引/配置 当前状态（JSON）",
    "everything://presets": "预设清单（JSON）",
}


def _markdown_for(help_payload: dict) -> str:
    """把结构化帮助渲染成 Markdown。"""
    lines: list[str] = [
        "# everything-mcp 能力矩阵与工具清单",
        "",
        f"- 服务器版本：`{help_payload['version']}`",
        f"- 验证的 Everything 版本：`{help_payload['verified_everything_version']}`",
        "",
        "## 支持的搜索语法（1.4 实测）",
        "",
    ]
    for group, items in help_payload["syntax_supported"].items():
        lines.append(f"### {group}")
        lines.append("")
        for item in items:
            lines.append(f"- `{item}`")
        lines.append("")

    lines += ["## 不支持 / 有坑", ""]
    for group, items in help_payload["syntax_unsupported"].items():
        lines.append(f"### {group}")
        lines.append("")
        for item in items:
            lines.append(f"- {item}")
        lines.append("")

    lines += ["## 慢查询（需改 ini + 重建索引）", ""]
    for slow in help_payload["slow_queries"]:
        lines.append(f"- `{slow['syntax']}`：{slow['reason']}。处理：{slow['fix']}")
    lines.append("")

    http = help_payload["http_params"]
    lines += ["## HTTP JSON 参数", "", "### 支持", ""]
    for key, note in http["supported"].items():
        lines.append(f"- `{key}`：{note}")
    lines += ["", "### 忽略 / 不生效", ""]
    for key, note in http["ignored"].items():
        lines.append(f"- `{key}`：{note}")
    lines += ["", f"- 可用列：{', '.join(http['columns_supported'])}"]
    lines.append(f"- 不可用列：{', '.join(http['columns_unsupported'])}")
    lines.append(f"- 可用排序：{', '.join(http['sort_supported'])}")
    lines.append("")

    lines += ["## 工具", ""]
    for tool in help_payload["tools"]:
        params = "、".join(f"`{p}`" for p in tool["key_params"]) or "（无参数）"
        lines.append(
            f"- **{tool['name']}**（{tool['category']}）：{tool['purpose']}；参数：{params}"
        )
    lines.append("")

    lines += ["## 示例", ""]
    for example in help_payload["examples"]:
        arguments = ", ".join(
            f"{key}={value!r}" for key, value in example["arguments"].items()
        )
        lines.append(f"- {example['intent']} → `{example['tool']}({arguments})`")
        if example.get("note"):
            lines.append(f"  - 说明：{example['note']}")
    lines.append("")

    lines += ["## 提示", ""]
    for tip in help_payload["tips"]:
        lines.append(f"- {tip}")
    lines += ["", "## MCP 资源", ""]
    for uri, purpose in help_payload["resource_uris"].items():
        lines.append(f"- `{uri}`：{purpose}")
    lines.append("")
    return "\n".join(lines)


def build_help() -> dict:
    """构造完整帮助载荷（结构化 + markdown）。"""
    payload = {
        "version": __version__,
        "verified_everything_version": VERIFIED_EVERYTHING_VERSION,
        "syntax_supported": SYNTAX_SUPPORTED,
        "syntax_unsupported": SYNTAX_UNSUPPORTED,
        "slow_queries": SLOW_QUERIES,
        "http_params": HTTP_PARAMS,
        "tools": TOOLS,
        "examples": EXAMPLES,
        "tips": TIPS,
        "resource_uris": RESOURCE_URIS,
    }
    payload["markdown"] = _markdown_for(payload)
    return payload


def render_syntax_markdown() -> str:
    """``everything://help/syntax`` 资源内容：只渲染能力矩阵部分。"""
    help_payload = build_help()
    lines: list[str] = [
        "# Everything 搜索语法能力矩阵",
        "",
        f"服务器版本 `{help_payload['version']}`；事实来源：Everything "
        f"`{help_payload['verified_everything_version']}` + 内置 HTTP Server（2026-10-18 实测）。",
        "",
        "## 支持",
        "",
    ]
    for group, items in SYNTAX_SUPPORTED.items():
        lines.append(f"### {group}")
        lines.append("")
        for item in items:
            lines.append(f"- `{item}`")
        lines.append("")
    lines += ["## 不支持 / 有坑", ""]
    for group, items in SYNTAX_UNSUPPORTED.items():
        lines.append(f"### {group}")
        lines.append("")
        for item in items:
            lines.append(f"- {item}")
        lines.append("")
    lines += ["## 慢查询", ""]
    for slow in SLOW_QUERIES:
        lines.append(f"- `{slow['syntax']}`：{slow['reason']}。处理：{slow['fix']}")
    lines += ["", "## HTTP 参数（摘要）", ""]
    lines.append(f"- 支持排序：{', '.join(HTTP_PARAMS['sort_supported'])}")
    lines.append(f"- 支持列：{', '.join(HTTP_PARAMS['columns_supported'])}")
    lines.append(f"- 忽略参数：{', '.join(HTTP_PARAMS['ignored'])}")
    lines.append("")
    return "\n".join(lines)


def capability_summary() -> dict:
    """``everything_status`` 用的 HTTP 能力矩阵摘要（紧凑，随 §1 实测更新）。"""
    return {
        "columns": list(SUPPORTED_COLUMN_KEYS),
        "columns_unsupported": HTTP_PARAMS["columns_unsupported"],
        "sort": list(SUPPORTED_SORT_KEYS),
        "sort_fallback": "name",
        "default_ascending": True,
        "sort_note": "只支持 name/path/size/date_modified；其余静默回退 name 升序；"
        "name 为 Everything 自然排序（非码点序）",
        "count": {
            "max": None,
            "note": "HTTP count 无硬上限（1000/2000/5000/10000 均按请求返回）；"
            "count=0 只回 total；负数/非法 → 0 条；"
            "本服务器仍按 EV_MAX_RESULTS_CAP 限制单次返回",
        },
        "match_path_param": (
            "HTTP 参数 **path=1**（仅布尔真值 1）是 match-path 搜索开关"
            "（search=blender&path=1 → 620→22931）；matchpath=1 无效，"
            "path=<路径>/path=true/filter 均无效；路径范围用搜索语法 path:\"...\""
        ),
        "size_semantics": (
            "1.4.1 实测：size:Nkb = ceil(size/1024)==N（size:1kb 命中 1..1024B、"
            "size:2kb 命中 1025..2048B）；size:Nmb / size:Ngb 先换算成 KB 再向上取整"
            "（size:1mb 命中 1047553..1048576B，不是精确 1MB；size:1gb 同理）；"
            "size:N / size:Nb 字节精确；size:Nk / size:Nm（无 b）的 k/m 被忽略 = N 字节；"
            "size:0kb=0 条；精确比较用无单位字节或 range（size:1024..2048 含端点）"
        ),
        "dupe": {
            "supported": True,
            "slow": True,
            "note": "dupe: 同名重复在 1.4.1 可用，全盘约 180–240ms（比普通查询慢两个数量级）",
        },
        "http_ignored": list(HTTP_PARAMS["ignored"]),
        "syntax_unsupported": [
            item for items in SYNTAX_UNSUPPORTED.values() for item in items
        ],
        "slow_syntax": [item["syntax"] for item in SLOW_QUERIES],
        "resources": list(RESOURCE_URIS),
    }
