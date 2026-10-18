"""Everything 预设查询（``everything_presets`` 工具与 ``everything://presets`` 资源）。

设计：
- 内置预设见 SPEC §3.1 第 5 条，开箱即用，全部使用 **1.4 实测可用**的语法；
  不使用 ``sort:``/``run_count:``/``distinct:`` 等 1.5 特性；``dupe:` 虽在
  1.4.1 可用（同名重复），但全盘约 180–240ms、比普通查询慢两个数量级，
  **默认预设不使用**，重复文件请走 ``everything_duplicates`` 工具。
- ``EV_PRESETS_FILE`` 可覆盖同名预设或追加新预设，支持 JSON 与 YAML：
  * JSON 需要 Python 标准库即可；
  * YAML 需要 ``PyYAML``；本机若未安装，会给出“请改用 JSON”的可操作告警，
    **不会崩溃**。
- 文件结构：``{"presets": [ {name, description, query, regex, path_scope,
  sort, ascending, max_results, fields}, ... ]}`` 或直接是数组；
- 预设文本支持占位符 ``{downloads}`` / ``{userprofile}`` / ``{workspace}``，
  运行/展示时按当前环境替换（``Downloads`` 目录随用户不同）。

所有函数都是只读的：不写预设文件、不调用 Everything。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from .config import Config

logger = logging.getLogger("everything_mcp.presets")

# 预设文件大小上限（防止误读大文件）
MAX_PRESETS_FILE_BYTES = 1024 * 1024
# 预设 name 允许的字符（工具入参，限制为安全 ASCII 片段）
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
PRESET_KEYS = {
    "name",
    "description",
    "query",
    "regex",
    "path_scope",
    "sort",
    "ascending",
    "max_results",
    "fields",
}


def _preset(
    name: str,
    description: str,
    query: str,
    *,
    regex: bool = False,
    path_scope: str = "",
    sort: str = "",
    ascending: bool | None = None,
    max_results: int = 100,
    fields: list[str] | None = None,
) -> dict:
    return {
        "name": name,
        "description": description,
        "query": query,
        "regex": regex,
        "path_scope": path_scope,
        "sort": sort,
        "ascending": ascending,
        "max_results": max_results,
        "fields": fields,
    }


#: 内置预设（SPEC §3.1 第 5 条）。全部只用 1.4 已验证语法。
BUILTIN_PRESETS: list[dict] = [
    _preset(
        "recent_modified_7d",
        "最近 7 天修改过的文件/目录",
        "dm:last7days",
        sort="date_modified",
        ascending=False,
    ),
    _preset(
        "today_changed",
        "今天修改过的文件/目录",
        "dm:today",
        sort="date_modified",
        ascending=False,
    ),
    _preset(
        "large_files_100mb",
        "大于 100MB 的大文件",
        "size:>100mb",
        sort="size",
        ascending=False,
    ),
    _preset(
        "screenshots_7d",
        "最近 7 天的截图/图片（png/jpg/jpeg/bmp/gif/webp）",
        "dm:last7days ext:png;jpg;jpeg;bmp;gif;webp",
        sort="date_modified",
        ascending=False,
    ),
    _preset(
        "archives_100mb",
        "大于 100MB 的压缩包/镜像（zip/7z/rar/tar/gz/xz/bz2/iso/cab）",
        "size:>100mb ext:zip;7z;rar;tar;gz;xz;bz2;iso;cab",
        sort="size",
        ascending=False,
    ),
    _preset(
        "git_repos",
        "Git 仓库（名为 .git 的目录）",
        "folder:.git",
        sort="path",
        ascending=True,
        max_results=200,
    ),
    _preset(
        "node_modules",
        "node_modules 目录",
        "folder:node_modules",
        sort="path",
        ascending=True,
        max_results=200,
    ),
    _preset(
        "downloads_today",
        "今天下载的文件（当前用户的 Downloads 目录）",
        "dm:today",
        path_scope="{downloads}",
        sort="date_modified",
        ascending=False,
    ),
]


# ----------------------------------------------------------------------
# 占位符替换
# ----------------------------------------------------------------------
def _placeholders(config: Config) -> dict[str, str]:
    user = str(Path.home())
    return {
        "downloads": str(Path(user) / "Downloads"),
        "userprofile": user,
        "workspace": str(config.workspace_root),
    }


def substitute(text: str, config: Config) -> str:
    """替换 ``{downloads}`` / ``{userprofile}`` / ``{workspace}`` 占位符。

    不用 ``str.format``，避免破坏正则里的 ``{2}`` 等量词。
    """
    if not isinstance(text, str):
        return text
    for key, value in _placeholders(config).items():
        text = text.replace("{" + key + "}", value)
    return text


def materialize(preset: dict, config: Config) -> dict:
    """返回占位符已替换的预设副本（不修改内置数据）。"""
    result = dict(preset)
    for key in ("query", "path_scope"):
        if isinstance(result.get(key), str):
            result[key] = substitute(result[key], config)
    return result


# ----------------------------------------------------------------------
# 校验 / 解析
# ----------------------------------------------------------------------
def _coerce_preset(raw: Any, source: str, warnings: list[str]) -> dict | None:
    """校验单条预设；非法时返回 None 并追加可操作告警。"""
    if not isinstance(raw, dict):
        warnings.append(f"{source}：预设条目必须是对象，已忽略：{raw!r}")
        return None
    unknown = set(raw) - PRESET_KEYS
    if unknown:
        warnings.append(
            f"{source}：预设 {raw.get('name')!r} 含未知字段 {sorted(unknown)}，已忽略这些字段。"
        )
    name = str(raw.get("name") or "").strip()
    if not name:
        warnings.append(f"{source}：预设缺少 name 字段，已忽略。")
        return None
    if not NAME_PATTERN.match(name):
        warnings.append(
            f"{source}：预设名 {name!r} 不合法（仅允许字母/数字/下划线/点/短横线，"
            "长度 1-64），已忽略。"
        )
        return None
    query = raw.get("query")
    if not isinstance(query, str):
        warnings.append(f"{source}：预设 {name!r} 的 query 必须是字符串，已忽略。")
        return None

    def _as_str(key: str) -> str:
        value = raw.get(key, "")
        if value is None:
            return ""
        if not isinstance(value, str):
            warnings.append(f"{source}：预设 {name!r} 的 {key} 应为字符串，已转为字符串。")
            return str(value)
        return value

    max_results = raw.get("max_results", 100)
    if not isinstance(max_results, int) or isinstance(max_results, bool) or max_results <= 0:
        warnings.append(
            f"{source}：预设 {name!r} 的 max_results 必须是正整数，已回退 100。"
        )
        max_results = 100
    fields_raw = raw.get("fields")
    fields: list[str] | None = None
    if fields_raw is not None:
        if isinstance(fields_raw, str):
            fields_raw = [part.strip() for part in fields_raw.split(",") if part.strip()]
        if isinstance(fields_raw, list) and all(isinstance(item, str) for item in fields_raw):
            fields = [item for item in fields_raw]
        else:
            warnings.append(
                f"{source}：预设 {name!r} 的 fields 必须是字符串数组，已忽略该字段。"
            )
    ascending = raw.get("ascending")
    if ascending is not None and not isinstance(ascending, bool):
        warnings.append(f"{source}：预设 {name!r} 的 ascending 应为布尔值，已忽略。")
        ascending = None
    regex = raw.get("regex", False)
    if not isinstance(regex, bool):
        warnings.append(f"{source}：预设 {name!r} 的 regex 应为布尔值，已按 False 处理。")
        regex = False

    return {
        "name": name,
        "description": _as_str("description"),
        "query": query,
        "regex": regex,
        "path_scope": _as_str("path_scope"),
        "sort": _as_str("sort"),
        "ascending": ascending,
        "max_results": max_results,
        "fields": fields,
    }


def _parse_text(text: str, path: Path) -> tuple[list[dict], list[str]]:
    """按扩展名/内容解析 JSON 或 YAML。返回 (原始条目列表, 告警)。"""
    suffix = path.suffix.lower()
    warnings: list[str] = []
    data: Any = None
    if suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            return [], [f"预设文件 JSON 解析失败（{path}）：{exc.msg}（第 {exc.lineno} 行）"]
    elif suffix in {".yaml", ".yml"}:
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError:
            return [], [
                f"预设文件 {path} 是 YAML 格式，但当前 Python 未安装 PyYAML。"
                "请改用 JSON 格式的预设文件（扩展名 .json），或安装 PyYAML 后重试。"
            ]
        try:
            data = yaml.safe_load(text)
        except Exception as exc:  # noqa: BLE001 - YAML 解析错误类型因版本而异
            return [], [f"预设文件 YAML 解析失败（{path}）：{exc}"]
    else:
        # 无扩展名/其他扩展名：先 JSON，再尝试 YAML
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            try:
                import yaml  # type: ignore[import-not-found]
            except ImportError:
                return [], [
                    f"预设文件 {path} 无法按 JSON 解析，且未安装 PyYAML；"
                    "请改用 .json 文件。"
                ]
            try:
                data = yaml.safe_load(text)
            except Exception as exc:  # noqa: BLE001
                return [], [f"预设文件解析失败（{path}）：{exc}"]

    if isinstance(data, dict):
        raw_items = data.get("presets")
        if raw_items is None:
            return [], [
                f"预设文件 {path} 的结构应为 {{\"presets\": [...]}} 或数组，"
                "未找到 presets 字段。"
            ]
    else:
        raw_items = data
    if not isinstance(raw_items, list):
        return [], [f"预设文件 {path} 的 presets 必须是数组。"]
    return raw_items, warnings


def _load_file(path: Path) -> tuple[list[dict], list[str]]:
    try:
        size = path.stat().st_size
    except OSError as exc:
        return [], [f"无法读取预设文件 {path}：{exc}"]
    if size > MAX_PRESETS_FILE_BYTES:
        return [], [
            f"预设文件过大（{size} 字节 > {MAX_PRESETS_FILE_BYTES}），已忽略：{path}"
        ]
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        return [], [f"无法读取预设文件 {path}：{exc}"]

    warnings: list[str] = []
    raw_items, parse_warnings = _parse_text(text, path)
    warnings.extend(parse_warnings)
    presets: list[dict] = []
    for raw in raw_items:
        item = _coerce_preset(raw, str(path), warnings)
        if item is not None:
            presets.append(item)
    return presets, warnings


# ----------------------------------------------------------------------
# 对外接口
# ----------------------------------------------------------------------
def load_presets(config: Config) -> tuple[list[dict], list[str]]:
    """返回 ``(预设列表, 告警列表)``；自定义预设覆盖同名内置预设、否则追加。"""
    merged: dict[str, dict] = {}
    for item in BUILTIN_PRESETS:
        merged[item["name"]] = materialize(item, config)
    warnings: list[str] = []
    path = config.presets_file
    if path is not None:
        if not path.is_file():
            warnings.append(
                f"EV_PRESETS_FILE 指向的文件不存在：{path}（当前仅使用内置预设）。"
            )
        else:
            loaded, file_warnings = _load_file(path)
            warnings.extend(file_warnings)
            for item in loaded:
                item = materialize(item, config)
                merged[item["name"]] = item
    return list(merged.values()), warnings


def get_preset(name: str, config: Config) -> tuple[dict | None, list[str]]:
    """按名称取预设；不存在时返回 ``(None, warnings)``。"""
    presets, warnings = load_presets(config)
    target = str(name or "").strip()
    for item in presets:
        if item["name"] == target:
            return item, warnings
    return None, warnings


def available_names(config: Config) -> list[str]:
    presets, _ = load_presets(config)
    return [item["name"] for item in presets]


def describe(config: Config) -> dict:
    """供 ``everything_presets`` 工具与 ``everything://presets`` 资源使用。"""
    presets, warnings = load_presets(config)
    custom_count = 0
    builtin_names = {item["name"] for item in BUILTIN_PRESETS}
    for item in presets:
        if item["name"] not in builtin_names:
            custom_count += 1
    return {
        "count": len(presets),
        "builtin_count": len(BUILTIN_PRESETS),
        "custom_count": custom_count,
        "custom_file": str(config.presets_file) if config.presets_file else None,
        "presets": presets,
        "warnings": warnings,
    }


def build_search_kwargs(
    preset: dict,
    config: Config,
    *,
    max_results: int | None = None,
    offset: int = 0,
    path_scope: str = "",
) -> dict:
    """把预设 + 调用方覆盖项转换成 ``EverythingClient.search`` 的关键字参数。"""
    preset = materialize(preset, config)
    kwargs: dict[str, Any] = {
        "regex": bool(preset.get("regex")),
        "max_results": preset.get("max_results") or 100,
        "offset": offset,
        "sort": preset.get("sort") or "",
        "path_scope": path_scope or preset.get("path_scope") or "",
        "fields": preset.get("fields"),
    }
    ascending = preset.get("ascending")
    if isinstance(ascending, bool):
        kwargs["ascending"] = ascending
    if isinstance(max_results, int) and max_results > 0:
        kwargs["max_results"] = max_results
    return kwargs
