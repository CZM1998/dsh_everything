"""everything-mcp 组合能力层：summary / duplicates / grep / batch_stat / changes / reveal。

设计约束（SPEC §2/§3.2/§5）：

- 本模块 **import 零副作用**：不联网、不读大文件、不启动子进程、不抛异常。
  唯一的注册入口是 ``register(mcp, get_client)``，由 ``server.py`` 调用。
- 只依赖 ``client.py`` 的契约方法（``search`` / ``count`` / ``search_pages``），
  通过 ``inspect.signature`` 做能力探测：task-3 的 v2 client 优先使用
  ``offset/path_scope/file_only/folder_only/fields``；若运行的是 v1 client，
  自动退化为 ``path_filter`` + 查询串追加 ``file:``/``folder:``，并把降级写进 ``warnings``。
- **只读安全**：除 ``everything_reveal``（explorer 打开/定位，受
  ``EV_ALLOW_SIDE_EFFECTS`` 控制）外，任何工具都不写/删/移/重命名文件。
- 每个工具都有硬上限：summary 限 ``max_scan``（默认且不超过 ``EV_SCAN_CAP``）、
  duplicates 限 ``max_files`` / ``max_hash_bytes``、grep 限 ``max_results`` / ``timeout_ms``、
  batch_stat 限 200 条路径、changes 限 ``max_results``。
- 错误信息为可操作中文；全部通过 ``ComposeError``（RuntimeError 子类）抛出，
  FastMCP 会把它作为工具错误返回。
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import ntpath
import os
import re
import stat as stat_mod
import subprocess
import time
import weakref
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import rg

logger = logging.getLogger("everything_mcp.compose_tools")

VERSION = "2.0.0"

# 工具名（供 help/status/测试引用）
TOOL_NAMES = (
    "everything_summary",
    "everything_duplicates",
    "everything_grep",
    "everything_batch_stat",
    "everything_changes",
    "everything_reveal",
)

SUMMARY_GROUPS = ("ext", "dir", "type", "size_bucket", "month", "parent")
DUP_MODES = ("name", "name_size", "size", "hash")
HASH_ALGOS = ("md5", "sha1", "sha256", "sha512", "blake2b")
DEFAULT_HASH_MAX_BYTES = 268435456
DEFAULT_SCAN_CAP = 50000
MAX_BATCH_PATHS = 200
MAX_GREP_RESULTS = 1000
MAX_CHANGES_RESULTS = 1000
MAX_DUPS_MAX_FILES = 200000
MAX_DUPS_MAX_GROUPS = 50
#: duplicates 默认响应体积预算（Lead 要求默认 <4KB；0 表示不限制）
DEFAULT_DUPS_RESPONSE_BYTES = 4096

# 搜索时请求的字段（v2 client）；v1 client 会退化为 columns
SCAN_FIELDS = ("name", "path", "size", "date_modified", "type", "extension")


class ComposeError(RuntimeError):
    """组合工具的可操作错误（中文消息）。

    ``kind``：``bad_argument`` / ``side_effects_disabled`` / ``not_found`` /
    ``rg_missing`` / ``client_error`` / ``error``。
    """

    def __init__(self, message: str, *, kind: str = "error", details: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.details = details or {}


# ----------------------------------------------------------------------
# 通用小工具
# ----------------------------------------------------------------------
def _env_text(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    text = raw.strip()
    return text or None


def _to_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    return int(str(value).strip())


def _to_float(value: Any) -> float:
    return float(str(value).strip())


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text not in {"0", "false", "no", "off", "disabled", ""}


def _config_value(client: Any, attr: str, env_name: str, default: Any, cast: Callable[[Any], Any]):
    """读取配置：优先 client.config.<attr>（v2），否则环境变量，最后默认值。"""
    config = getattr(client, "config", None)
    if config is not None:
        raw = None
        if isinstance(config, dict):
            raw = config.get(attr)
        else:
            raw = getattr(config, attr, None)
        if raw is not None:
            try:
                return cast(raw)
            except (TypeError, ValueError):
                pass
    raw = _env_text(env_name)
    if raw is None:
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


def _clamp_int(
    value: Any,
    low: int,
    high: int,
    default: int,
    warnings: list[str] | None = None,
    name: str = "",
) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
        if warnings is not None and value is not None:
            warnings.append(f"{name or '参数'}={value!r} 不是整数，已使用默认值 {default}。")
        return max(low, min(high, default))
    clamped = max(low, min(high, number))
    if warnings is not None and name and clamped != number:
        warnings.append(f"{name}={number} 超出允许范围 [{low}, {high}]，已按 {clamped} 处理。")
    return clamped


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        text = str(value).strip().replace(",", "")
        return int(text)
    except (TypeError, ValueError):
        return None


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00").replace("z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _search_params(client: Any) -> set[str]:
    try:
        return set(inspect.signature(client.search).parameters)
    except (TypeError, ValueError):
        return set()


def _apply_type_filters(
    query: str,
    params: set[str],
    file_only: bool,
    folder_only: bool,
    warnings: list[str],
) -> str:
    if file_only and folder_only:
        raise ComposeError("file_only 与 folder_only 不能同时为 true。", kind="bad_argument")
    effective = (query or "").strip()
    if file_only and "file_only" not in params:
        effective = f"{effective} file:".strip()
        warnings.append("当前 client 无 file_only 参数，已在查询中追加 file: 过滤。")
    if folder_only and "folder_only" not in params:
        effective = f"{effective} folder:".strip()
        warnings.append("当前 client 无 folder_only 参数，已在查询中追加 folder: 过滤。")
    return effective


def _prepare_search(
    client: Any,
    query: str,
    *,
    path_scope: str = "",
    file_only: bool = False,
    folder_only: bool = False,
    sort: str = "",
    ascending: bool | None = None,
    fields: Any = None,
) -> tuple[str, dict, list[str]]:
    """把组合工具的参数转换为 client.search 支持的 kwargs，并记录降级 warnings。"""
    params = _search_params(client)
    warnings: list[str] = []
    effective = _apply_type_filters(query, params, file_only, folder_only, warnings)
    kwargs: dict[str, Any] = {}

    scope = str(path_scope or "").strip()
    if scope:
        if "path_scope" in params:
            kwargs["path_scope"] = scope
        elif "path_filter" in params:
            kwargs["path_filter"] = scope
            warnings.append(
                "当前 client 无 path_scope 参数，已退化为客户端 path_filter"
                "（可能只覆盖前若干条候选，建议升级 client）。"
            )
        else:
            warnings.append("当前 client 不支持路径范围过滤，path_scope 被忽略。")
    if file_only and "file_only" in params:
        kwargs["file_only"] = True
    if folder_only and "folder_only" in params:
        kwargs["folder_only"] = True
    if sort:
        if "sort" in params:
            kwargs["sort"] = str(sort)
        else:
            warnings.append(f"当前 client 不支持 sort={sort!r}，排序被忽略。")
    if ascending is not None:
        if "ascending" in params:
            kwargs["ascending"] = bool(ascending)
        else:
            warnings.append("当前 client 不支持 ascending 参数，排序方向使用默认值。")
    if fields:
        field_list = [str(item) for item in fields]
        if "fields" in params:
            kwargs["fields"] = field_list
        elif "columns" in params:
            columns = [
                name
                for name in ("size", "date_modified", "date_created", "date_accessed")
                if name in field_list
            ]
            if columns:
                kwargs["columns"] = columns
        else:
            warnings.append("当前 client 不支持 fields/columns，结果可能缺少 size/date_modified。")
    return effective, kwargs, warnings


def _scan(
    client: Any,
    query: str,
    *,
    max_scan: int,
    page_size: int = 1000,
    path_scope: str = "",
    file_only: bool = False,
    folder_only: bool = False,
    sort: str = "",
    ascending: bool | None = None,
    fields: Any = SCAN_FIELDS,
) -> dict:
    """分页扫描 Everything 命中结果（page_size <= 1000，最多 max_scan 条）。

    返回 ``{items,total,scanned,truncated,effective_query,warnings,pages}``。
    """
    warnings: list[str] = []
    effective, kwargs, degraded = _prepare_search(
        client,
        query,
        path_scope=path_scope,
        file_only=file_only,
        folder_only=folder_only,
        sort=sort,
        ascending=ascending,
        fields=fields,
    )
    warnings.extend(degraded)
    params = _search_params(client)
    limit = max(1, int(max_scan))
    page_size = max(1, min(int(page_size), 1000))
    items: list[dict] = []
    total = 0
    offset = 0
    pages = 0
    truncated = False

    while len(items) < limit:
        want = min(page_size, limit - len(items))
        call_kwargs = dict(kwargs)
        call_kwargs["max_results"] = want
        if "offset" in params:
            call_kwargs["offset"] = offset
        elif offset:
            break
        data = client.search(effective, **call_kwargs)
        if not isinstance(data, dict):
            raise ComposeError(
                "client.search 返回了非字典结构，无法完成组合统计（请检查 client 版本）。",
                kind="client_error",
            )
        total = _as_int(data.get("total")) or 0
        batch = data.get("results") or []
        if not isinstance(batch, list) or not batch:
            break
        for item in batch:
            if isinstance(item, dict):
                items.append(item)
        pages += 1
        offset += len(batch)
        if "offset" not in params:
            break
        if offset >= total:
            break

    if len(items) > limit:
        items = items[:limit]
    if total > len(items) or (len(items) >= limit and offset < total):
        truncated = True
    return {
        "items": items,
        "total": total,
        "scanned": len(items),
        "truncated": truncated,
        "effective_query": effective,
        "warnings": warnings,
        "pages": pages,
    }


def _search_once(
    client: Any,
    query: str,
    *,
    max_results: int = 100,
    path_scope: str = "",
    file_only: bool = False,
    folder_only: bool = False,
    sort: str = "",
    ascending: bool | None = None,
    fields: Any = None,
) -> tuple[dict, list[str]]:
    effective, kwargs, warnings = _prepare_search(
        client,
        query,
        path_scope=path_scope,
        file_only=file_only,
        folder_only=folder_only,
        sort=sort,
        ascending=ascending,
        fields=fields,
    )
    params = _search_params(client)
    if "offset" in params:
        kwargs["offset"] = 0
    kwargs["max_results"] = max(1, min(int(max_results), 1000))
    data = client.search(effective, **kwargs)
    if not isinstance(data, dict):
        raise ComposeError("client.search 返回了非字典结构。", kind="client_error")
    return data, warnings


# ----------------------------------------------------------------------
# everything_summary
# ----------------------------------------------------------------------
def _normalize_scope_prefix(scope: str) -> str:
    """把 path_scope 归一化为用于相对路径计算的绝对前缀（反斜杠、无尾斜杠、小写）。"""
    text = str(scope or "").strip()
    if not text:
        return ""
    try:
        absolute = os.path.abspath(os.path.expanduser(text))
    except OSError:
        absolute = text
    return absolute.replace("/", "\\").rstrip("\\").lower()


def _dir_key(path: str, depth: int, scope_prefix: str = "") -> str:
    """dir 分组键：相对 path_scope 的子目录链；无 scope 时取绝对路径尾部 N 段。

    - 有 ``path_scope`` 且路径在其下：``(root)`` = scope 直接下的文件；
      ``a`` = ``<scope>\\a\\file``（depth>=1）；``a\\b`` = 两级。
    - 无 scope：取绝对路径目录部分最后 ``depth`` 段（如 ``b\\c``）。
    """
    text = str(path or "").strip()
    if not text:
        return "(unknown)"
    normalized = text.replace("/", "\\")
    lowered = normalized.lower()
    if scope_prefix and (lowered == scope_prefix or lowered.startswith(scope_prefix + "\\")):
        relative = normalized[len(scope_prefix):].lstrip("\\")
        parts = [part for part in relative.split("\\") if part]
        dirs = parts[:-1]  # 去掉文件（或文件夹自身），保留相对父目录链
        if not dirs:
            return "(root)"
        return "\\".join(dirs[: max(1, depth)])
    parts = [part for part in normalized.split("\\") if part]
    if not parts:
        return "(unknown)"
    has_drive = bool(re.match(r"^[A-Za-z]:$", parts[0]))
    dirs = parts[1:-1] if has_drive else parts[:-1]
    if not dirs:
        return parts[0] + "\\" if has_drive else "(root)"
    return "\\".join(dirs[-max(1, depth):])


def _size_bucket(size: int | None) -> str:
    if size is None:
        return "(unknown)"
    if size == 0:
        return "0 B"
    if size < 1024:
        return "<1 KB"
    if size < 1024**2:
        return "1 KB–1 MB"
    if size < 10 * 1024**2:
        return "1–10 MB"
    if size < 100 * 1024**2:
        return "10–100 MB"
    if size < 1024**3:
        return "100 MB–1 GB"
    return "≥1 GB"


def _summary_key(item: dict, group_by: str, dir_depth: int, scope_prefix: str = "") -> str:
    path = str(item.get("path") or "")
    name = str(item.get("name") or ntpath.basename(path) or "")
    if group_by == "ext":
        ext = item.get("extension")
        if not ext and name:
            dot = name.rfind(".")
            ext = name[dot + 1:].lower() if 0 < dot < len(name) - 1 else ""
        ext = str(ext or "").lower().lstrip(".")
        return ext or "(none)"
    if group_by == "type":
        return str(item.get("type") or "file").lower()
    if group_by == "dir":
        return _dir_key(path, dir_depth, scope_prefix)
    if group_by == "parent":
        return ntpath.dirname(path) or "(unknown)"
    if group_by == "size_bucket":
        return _size_bucket(_as_int(item.get("size")))
    if group_by == "month":
        moment = _parse_iso(item.get("date_modified"))
        return moment.strftime("%Y-%m") if moment else "(unknown)"
    return "(unknown)"


def summary_impl(
    client: Any,
    *,
    query: str = "",
    path_scope: str = "",
    group_by: str = "ext",
    dir_depth: int = 2,
    top_n: int = 20,
    max_scan: int = 50000,
    include_folders: bool = False,
) -> dict:
    started = time.perf_counter()
    warnings: list[str] = []
    normalized_group = str(group_by or "ext").strip().lower()
    aliases = {"extension": "ext", "extensions": "ext", "directory": "dir", "size": "size_bucket"}
    normalized_group = aliases.get(normalized_group, normalized_group)
    if normalized_group not in SUMMARY_GROUPS:
        raise ComposeError(
            f"不支持的 group_by={group_by!r}。可用值：{', '.join(SUMMARY_GROUPS)}。",
            kind="bad_argument",
        )
    dir_depth = _clamp_int(dir_depth, 1, 12, 2, warnings, "dir_depth")
    top_n = _clamp_int(top_n, 1, 200, 20, warnings, "top_n")
    scan_cap = max(1, _config_value(client, "scan_cap", "EV_SCAN_CAP", DEFAULT_SCAN_CAP, _to_int))
    scan_limit = _clamp_int(max_scan, 1, scan_cap, scan_cap, warnings, "max_scan")
    include_folders = bool(include_folders)

    # Everything HTTP 1.4 对空查询/无类型过滤的升序排序是“目录优先”（前 N 条可能
    # 全是目录，size 恒为 0）。因此默认显式只扫文件（size 有意义），
    # include_folders=True 时再按独立预算补扫目录；用户 query 里已显式带
    # file:/folder: 时尊重原查询，不再强行追加类型过滤。
    query_text = str(query or "")
    explicit_type_filter = bool(re.search(r"(?:^|\s)(?:file|folder):", query_text, re.IGNORECASE))
    folder_scanned = 0
    if explicit_type_filter:
        scan = _scan(
            client,
            query_text,
            max_scan=scan_limit,
            page_size=1000,
            path_scope=path_scope,
            sort="path",
            ascending=True,
            fields=SCAN_FIELDS,
        )
        warnings.extend(scan["warnings"])
        items = list(scan["items"])
        total = scan["total"]
        truncated = bool(scan["truncated"])
        aggregates = "query"
    else:
        file_scan = _scan(
            client,
            query_text,
            max_scan=scan_limit,
            page_size=1000,
            path_scope=path_scope,
            file_only=True,
            sort="path",
            ascending=True,
            fields=SCAN_FIELDS,
        )
        warnings.extend(file_scan["warnings"])
        items = list(file_scan["items"])
        total = file_scan["total"]
        truncated = bool(file_scan["truncated"])
        aggregates = "files"
        if include_folders:
            folder_budget = min(scan_limit, 5000)
            folder_scan = _scan(
                client,
                query_text,
                max_scan=folder_budget,
                page_size=1000,
                path_scope=path_scope,
                folder_only=True,
                sort="name",
                ascending=True,
                fields=SCAN_FIELDS,
            )
            warnings.extend(folder_scan["warnings"])
            items.extend(folder_scan["items"])
            total += folder_scan["total"]
            folder_scanned = len(folder_scan["items"])
            truncated = truncated or bool(folder_scan["truncated"])
            aggregates = "files+folders"
            if folder_scan["truncated"]:
                warnings.append(
                    f"目录扫描达到上限 {folder_budget}，目录计数可能不完整"
                    "（目录 size 恒为 0，主要影响 count）。"
                )
    scanned = len(items)
    if truncated:
        warnings.append(
            f"命中总数 {total} 超过扫描上限 {scanned}，"
            "统计仅覆盖已扫描部分（可提高 max_scan / EV_SCAN_CAP）。"
        )

    groups: dict[str, dict] = {}
    size_sum = 0
    size_min: int | None = None
    size_max: int | None = None
    scope_prefix = _normalize_scope_prefix(path_scope) if normalized_group == "dir" else ""
    for item in items:
        size = _as_int(item.get("size"))
        if size is not None:
            size_sum += size
            size_min = size if size_min is None else min(size_min, size)
            size_max = size if size_max is None else max(size_max, size)
        key = _summary_key(item, normalized_group, dir_depth, scope_prefix)
        bucket = groups.get(key)
        if bucket is None:
            bucket = {
                "key": key,
                "count": 0,
                "size": 0,
                "total_size": 0,
                "min_size": None,
                "max_size": None,
            }
            groups[key] = bucket
        bucket["count"] += 1
        if size is not None:
            bucket["size"] += size
            bucket["total_size"] += size
            bucket["min_size"] = size if bucket["min_size"] is None else min(bucket["min_size"], size)
            bucket["max_size"] = size if bucket["max_size"] is None else max(bucket["max_size"], size)

    ordered = sorted(
        groups.values(),
        key=lambda group: (-group["count"], -group["size"], str(group["key"])),
    )
    for group in ordered:
        group["avg_size"] = int(round(group["size"] / group["count"])) if group["count"] else 0
    returned = ordered[:top_n]

    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        "query": query,
        "path_scope": path_scope or None,
        "group_by": normalized_group,
        "dir_depth": dir_depth if normalized_group == "dir" else None,
        "dir_grouping": (
            "relative_to_path_scope"
            if normalized_group == "dir" and scope_prefix
            else ("absolute_tail" if normalized_group == "dir" else None)
        ),
        "include_folders": include_folders,
        "aggregates": aggregates,
        "total": total,
        "scanned": scanned,
        "truncated_scan": bool(truncated),
        "groups": returned,
        "totals": {
            "count": scanned,
            "size": size_sum,
            "min": size_min,
            "max": size_max,
        },
        "stats": {
            "groups_found": len(ordered),
            "returned_groups": len(returned),
            "top_n": top_n,
            "max_scan": scan_limit,
            "folder_scanned": folder_scanned,
            "elapsed_ms": elapsed_ms,
        },
        "warnings": warnings,
        "elapsed_ms": elapsed_ms,
    }


# ----------------------------------------------------------------------
# everything_duplicates
# ----------------------------------------------------------------------
def _dedupe_items(items: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for item in items:
        path = str(item.get("path") or "")
        if not path:
            continue
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _hash_file(path: str, algo: str) -> str:
    digest = hashlib.new(algo)
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# dupe: 探测结果缓存（同进程内避免每次重复探测；Everything 1.4.1.1024 实测支持）
# 用 WeakKeyDictionary 绑定 client 实例，避免 id() 复用导致不同 client 命中错误缓存。
_DUPE_CACHE: "weakref.WeakKeyDictionary[Any, tuple[float, bool]]" = weakref.WeakKeyDictionary()
_DUPE_CACHE_TTL = 120.0


def _dupe_supported(client: Any) -> bool:
    """探测 Everything 是否支持 ``dupe:``（同名重复）查询。

    探测方式：``dupe:`` 单查 total>0 且无异常。结果按 client 实例缓存 120s，
    避免每次 duplicates 调用都多花一次全盘查询。
    """
    now = time.time()
    try:
        cached = _DUPE_CACHE.get(client)
    except TypeError:  # client 不可哈希/不可弱引用
        cached = None
    if cached and now - cached[0] < _DUPE_CACHE_TTL:
        return cached[1]
    supported = False
    try:
        data = client.search("dupe:", max_results=1, fields=("name",))
        if isinstance(data, dict):
            supported = (_as_int(data.get("total")) or 0) > 0
    except Exception as exc:  # noqa: BLE001 - 探测失败按“不支持”处理并回退分页扫描
        logger.warning("dupe: 探测失败，将回退分页扫描：%s", exc)
        supported = False
    try:
        _DUPE_CACHE[client] = (now, supported)
    except TypeError:
        pass
    return supported


def _dup_group(
    key: str,
    items: list[dict],
    *,
    size: int | None,
    hash_value: str | None,
    max_paths: int = 2,
) -> dict:
    """构造重复组：字段精简为 key/count/size?/hash?/path_count/paths_truncated/paths。

    ``paths`` 是路径字符串列表，默认只回前 ``max_paths`` 条（完整数量见 ``path_count``）。
    """
    paths: list[str] = []
    sizes: set[int] = set()
    path_count = 0
    for item in items:
        path = str(item.get("path") or "")
        if not path:
            continue
        path_count += 1
        item_size = _as_int(item.get("size"))
        if item_size is not None:
            sizes.add(item_size)
        if len(paths) < max_paths:
            paths.append(path)
    if size is None and len(sizes) == 1:
        size = next(iter(sizes))
    group: dict = {
        "key": key,
        "count": path_count,
    }
    if size is not None:
        group["size"] = size
    if hash_value is not None:
        group["hash"] = hash_value
    group["path_count"] = path_count
    group["paths_truncated"] = path_count > len(paths)
    group["paths"] = paths
    return group


def _duplicates_payload_size(result: dict) -> int:
    # 用 ASCII 转义（最坏情况）估算，确保调用方 json.dumps() 默认参数也 < 预算
    try:
        return len(json.dumps(result, ensure_ascii=True, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _shrink_duplicates_result(result: dict, budget: int) -> bool:
    """把 duplicates 响应压到 ``budget`` 字节内（默认 4096），返回是否发生截断。

    先把“压缩提示”warning/next_hint 计入体积，再依次：减少 paths（每次砍半）
    → 从尾部丢组 → 清空末组 paths；最后复算 next_hint 并再压一次，保证收敛。
    """
    if budget <= 0:
        return False
    if _duplicates_payload_size(result) <= budget:
        return False
    # 预留安全余量：实际压到 budget-192，避免边界数据波动后超出承诺体积
    target = max(512, budget - 192)
    stats = result.setdefault("stats", {})
    groups = result.get("groups") or []
    if not stats.get("payload_truncated"):
        stats["payload_truncated"] = True
        result["truncated"] = True
        result.setdefault("warnings", []).append(
            f"响应超过 max_response_bytes={budget}，已自动压缩；可设 max_response_bytes=0 关闭。"
        )
        result["next_hint"] = _duplicates_next_hint(result)

    def shrink_once() -> None:
        # 1) 减少 paths（每次砍半，保证收敛）
        while _duplicates_payload_size(result) > target:
            with_paths = [group for group in groups if group.get("paths")]
            if not with_paths:
                break
            chosen = max(with_paths, key=lambda group: len(group["paths"]))
            keep = len(chosen["paths"]) // 2
            chosen["paths"] = chosen["paths"][:keep]
            chosen["paths_truncated"] = chosen["path_count"] > len(chosen["paths"])
        # 2) 从尾部丢组
        while _duplicates_payload_size(result) > target and len(groups) > 1:
            groups.pop()
            result["truncated_groups"] = True
        # 3) 清空最后一组 paths
        while _duplicates_payload_size(result) > target and groups:
            group = groups[-1]
            if not group.get("paths"):
                if len(groups) == 1:
                    break
                groups.pop()
                result["truncated_groups"] = True
                continue
            group["paths"] = []
            group["paths_truncated"] = group["path_count"] > 0

    shrink_once()
    # next_hint 会因 paths_truncated/truncated_groups 变化，复算后再压一次
    result["next_hint"] = _duplicates_next_hint(result)
    shrink_once()
    stats["returned_groups"] = len(groups)
    stats["max_response_bytes"] = budget
    return True


def duplicates_impl(
    client: Any,
    *,
    mode: str = "name",
    query: str = "",
    path_scope: str = "",
    min_size: int = 0,
    max_groups: int = 10,
    max_files: int = 5000,
    hash_algo: str = "md5",
    max_hash_bytes: int = DEFAULT_HASH_MAX_BYTES,
    max_paths_per_group: int = 2,
    max_response_bytes: int = DEFAULT_DUPS_RESPONSE_BYTES,
) -> dict:
    started = time.perf_counter()
    warnings: list[str] = []
    normalized_mode = str(mode or "name").strip().lower()
    if normalized_mode not in DUP_MODES:
        raise ComposeError(
            f"不支持的 mode={mode!r}。可用值：{', '.join(DUP_MODES)}。",
            kind="bad_argument",
        )
    min_size = _clamp_int(min_size, 0, 10**15, 0, warnings, "min_size")
    max_groups = _clamp_int(max_groups, 1, MAX_DUPS_MAX_GROUPS, 10, warnings, "max_groups")
    max_files = _clamp_int(max_files, 2, MAX_DUPS_MAX_FILES, 5000, warnings, "max_files")
    max_paths = _clamp_int(max_paths_per_group, 1, 50, 2, warnings, "max_paths_per_group")
    response_budget = _clamp_int(max_response_bytes, 0, 1048576, DEFAULT_DUPS_RESPONSE_BYTES,
                                 warnings, "max_response_bytes")
    algo = str(hash_algo or "md5").strip().lower()
    if algo not in HASH_ALGOS:
        raise ComposeError(
            f"不支持的 hash_algo={hash_algo!r}。可用值：{', '.join(HASH_ALGOS)}。",
            kind="bad_argument",
        )
    config_hash_max = _config_value(
        client, "hash_max_bytes", "EV_HASH_MAX_BYTES", DEFAULT_HASH_MAX_BYTES, _to_int
    )
    hash_budget = _clamp_int(
        max_hash_bytes if max_hash_bytes is not None else config_hash_max,
        0,
        10**15,
        config_hash_max,
        warnings,
        "max_hash_bytes",
    )

    # 扫描顺序与候选来源：
    # - name / name_size：优先用 Everything 1.4 的 `dupe:` 查询直接拿同名候选
    #   （全盘约 180–240ms）；探测失败回退分页扫描。
    # - name：按 name 升序（同名文件在窗口内相邻）；
    # - name_size / size / hash：按 size 降序（同大小相邻、优先覆盖大文件）。
    dupe_used = False
    dupe_fallback = False
    scan_query = query
    if normalized_mode in ("name", "name_size"):
        if _dupe_supported(client):
            scan_query = f"dupe: {str(query).strip()}".strip()
            dupe_used = True
        else:
            dupe_fallback = True
            warnings.append(
                "Everything 1.4 的 dupe: 查询不可用（探测 total=0 或查询失败），"
                "已回退分页扫描；同名重复组可能不完整，建议提高 max_files。"
            )
    if normalized_mode == "name":
        scan_sort, scan_ascending = "name", True
    else:
        scan_sort, scan_ascending = "size", False
    scan = _scan(
        client,
        scan_query,
        max_scan=max_files,
        page_size=min(1000, max_files),
        path_scope=path_scope,
        file_only=True,
        sort=scan_sort,
        ascending=scan_ascending,
        fields=SCAN_FIELDS,
    )
    warnings.extend(scan["warnings"])
    items = _dedupe_items(scan["items"])
    truncated_files = bool(scan["truncated"] or scan["total"] > len(items))
    if truncated_files:
        warnings.append(
            f"仅扫描 {len(items)}/{scan['total']} 个文件（max_files={max_files}，{scan_sort}）；"
            "可调大 max_files。"
        )

    grouped: dict[str, list[dict]] = {}
    for item in items:
        if str(item.get("type") or "file").lower() == "folder":
            continue
        path = str(item.get("path") or "")
        if not path:
            continue
        name = str(item.get("name") or ntpath.basename(path) or "")
        size = _as_int(item.get("size"))
        if normalized_mode == "name":
            if min_size > 0 and size is not None and size < min_size:
                continue
            key = name.casefold()
        else:
            if size is None or size < min_size:
                continue
            key = f"{name.casefold()}|{size}" if normalized_mode == "name_size" else str(size)
        if not key:
            continue
        grouped.setdefault(key, []).append(item)

    candidates = [(key, group) for key, group in grouped.items() if len(group) >= 2]

    files_hashed = 0
    bytes_hashed = 0
    unreadable = 0
    budget_exhausted = False
    if normalized_mode == "hash":
        candidates.sort(key=lambda pair: (-len(pair[1]), -(_as_int(pair[1][0].get("size")) or 0)))
        confirmed: list[tuple[str, list[dict]]] = []
        for _key, group in candidates:
            if budget_exhausted:
                break
            by_hash: dict[str, list[dict]] = {}
            for item in group:
                path = str(item.get("path") or "")
                size = _as_int(item.get("size")) or 0
                if not path or not os.path.isfile(path):
                    unreadable += 1
                    continue
                if size > hash_budget or bytes_hashed + size > hash_budget:
                    budget_exhausted = True
                    break
                try:
                    digest = _hash_file(path, algo)
                except OSError:
                    unreadable += 1
                    continue
                files_hashed += 1
                bytes_hashed += size
                by_hash.setdefault(digest, []).append(item)
            if budget_exhausted:
                break
            for digest, same_hash in by_hash.items():
                if len(same_hash) >= 2:
                    confirmed.append((digest, same_hash))
        if budget_exhausted:
            warnings.append(
                f"哈希预算 max_hash_bytes={hash_budget} 已用尽（已哈希 {bytes_hashed} 字节）；"
                "可调大 max_hash_bytes。"
            )
        if unreadable:
            warnings.append(f"{unreadable} 个文件无法读取，已跳过。")
        groups = [
            _dup_group(
                digest,
                same_hash,
                size=_as_int(same_hash[0].get("size")),
                hash_value=digest,
                max_paths=max_paths,
            )
            for digest, same_hash in confirmed
        ]
    else:
        groups = []
        for _key, group in candidates:
            first = group[0]
            display = str(first.get("name") or ntpath.basename(str(first.get("path") or "")) or "")
            size = _as_int(first.get("size"))
            uniform = all(_as_int(item.get("size")) == size for item in group)
            if normalized_mode == "name_size":
                display = f"{display}|{size}"
            groups.append(
                _dup_group(
                    display,
                    group,
                    size=size if uniform else None,
                    hash_value=None,
                    max_paths=max_paths,
                )
            )

    groups.sort(key=lambda group: (-group["count"], -(group.get("size") or 0), str(group["key"])))
    groups_found = len(groups)
    truncated_groups = groups_found > max_groups
    returned = groups[:max_groups]
    if truncated_groups:
        warnings.append(
            f"仅返回前 {len(returned)}/{groups_found} 组（max_groups={max_groups}）；可调大 max_groups。"
        )

    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    stats: dict = {
        "scanned": len(items),
        "groups_found": groups_found,
        "returned_groups": len(returned),
        "max_files": max_files,
        "max_groups": max_groups,
        "max_paths_per_group": max_paths,
        "scan_order": f"{scan_sort}{' asc' if scan_ascending else ' desc'}",
        "dupe_query_used": scan_query,
        "dupe_support": dupe_used,
        "truncated": bool(truncated_files or truncated_groups),
    }
    if normalized_mode == "hash":
        stats.update(
            {
                "files_hashed": files_hashed,
                "bytes_hashed": bytes_hashed,
                "max_hash_bytes": hash_budget,
            }
        )
    result = {
        "mode": normalized_mode,
        "query": query,
        "query_used": scan_query,
        "path_scope": path_scope or None,
        "min_size": min_size,
        "total": scan["total"],
        "scanned": len(items),
        "truncated": bool(truncated_files or truncated_groups),
        "truncated_files": truncated_files,
        "truncated_groups": truncated_groups,
        "groups": returned,
        "stats": stats,
        "warnings": warnings,
        "elapsed_ms": elapsed_ms,
    }
    result["next_hint"] = _duplicates_next_hint(result)
    stats["payload_bytes"] = _duplicates_payload_size(result)
    # next_hint/warnings 也会计入体积：压缩后再按新 hint 复算，必要时多压几轮
    for _ in range(3):
        if not response_budget or _duplicates_payload_size(result) <= response_budget:
            break
        if not _shrink_duplicates_result(result, response_budget):
            break
        stats["truncated"] = True
        result["next_hint"] = _duplicates_next_hint(result)
    stats["payload_bytes"] = _duplicates_payload_size(result)
    return result


def _duplicates_next_hint(result: dict) -> str:
    """给模型的可操作下一步建议（尽量短，避免 token 膨胀）。"""
    stats = result.get("stats") or {}
    hints: list[str] = []
    if result.get("truncated_files"):
        hints.append(f"可调大 max_files（当前 {stats.get('max_files')}）或缩小 path_scope")
    if result.get("truncated_groups"):
        hints.append(f"可调大 max_groups（当前 {stats.get('max_groups')}，上限 50）看更多组")
    if stats.get("payload_truncated"):
        hints.append("响应已压缩；可设 max_response_bytes=0 关闭")
    if any(group.get("paths_truncated") for group in result.get("groups") or []):
        hints.append("每组仅回前几条 path，完整数量见 path_count")
    if not hints:
        hints.append("结果完整；可调大 max_groups/max_files/max_paths_per_group")
    return "；".join(hints)


# ----------------------------------------------------------------------
# everything_grep
# ----------------------------------------------------------------------
def _normalize_exts(exts: Any) -> tuple[list[str], list[str]]:
    """把 exts 归一化为 ['py','js']；非法项返回 warnings。"""
    warnings: list[str] = []
    if exts is None:
        return [], warnings
    if isinstance(exts, str):
        raw_items = re.split(r"[,;\s]+", exts)
    elif isinstance(exts, (list, tuple, set)):
        raw_items = [str(item) for item in exts]
    else:
        raw_items = [str(exts)]
    result: list[str] = []
    for raw in raw_items:
        text = str(raw).strip().lstrip(".").lower()
        if not text:
            continue
        if any(char in text for char in ("*", "?", "\\", "/", ":", "|")):
            warnings.append(f"忽略非法扩展名 {raw!r}（扩展名只写字母数字，如 py；复杂匹配请用 globs）。")
            continue
        if text not in result:
            result.append(text)
    return result, warnings


def _normalize_globs(globs: Any) -> list[str]:
    if globs is None:
        return []
    if isinstance(globs, str):
        return [globs] if globs.strip() else []
    return [str(item) for item in globs if str(item).strip()]


def _normalize_paths(paths: Any) -> list[str]:
    if paths is None:
        return []
    if isinstance(paths, str):
        return [paths] if paths.strip() else []
    return [str(item) for item in paths if str(item).strip()]


def _derive_grep_roots(
    client: Any, query: str, warnings: list[str]
) -> list[str]:
    """path_scope 为空时，从 Everything 候选文件推导搜索根（最多 8 个）。"""
    data, degrade = _search_once(
        client,
        query,
        max_results=500,
        file_only=True,
        fields=("path",),
    )
    warnings.extend(degrade)
    results = data.get("results") or []
    file_paths = [
        str(item.get("path") or "")
        for item in results
        if isinstance(item, dict) and str(item.get("type") or "file").lower() != "folder"
    ]
    file_paths = [path for path in file_paths if path]
    if not file_paths:
        raise ComposeError(
            "path_scope 为空，且 query 在 Everything 中没有可用文件结果，无法确定搜索根。\n"
            "请显式传入 path_scope（例如 D:\\Projects），或换一个能命中文件的 query。",
            kind="bad_argument",
        )
    parents: list[str] = []
    seen: set[str] = set()
    for path in file_paths:
        parent = ntpath.dirname(path)
        if not parent:
            continue
        key = os.path.normcase(parent)
        if key in seen:
            continue
        seen.add(key)
        parents.append(parent)
    if len(parents) <= 8:
        warnings.append(
            f"path_scope 为空：已从 query 命中的 {len(file_paths)} 个文件推导出 "
            f"{len(parents)} 个搜索根。"
        )
        return parents
    try:
        common = os.path.commonpath([os.path.abspath(path) for path in file_paths])
    except ValueError:
        common = ""
    drive_relative = os.path.splitdrive(common)[1].strip("\\/") if common else ""
    if common and drive_relative:
        warnings.append(
            f"候选文件分布在 {len(parents)} 个目录，已改用共同父目录 {common} 作为搜索根。"
        )
        return [common]
    warnings.append(
        f"候选文件分布在 {len(parents)} 个目录且无共同父目录，仅使用前 8 个目录作为搜索根；"
        "建议显式提供 path_scope 以获得完整结果。"
    )
    return parents[:8]


def grep_impl(
    client: Any,
    *,
    pattern: str,
    fixed_strings: bool = False,
    case_sensitive: bool = False,
    query: str = "",
    path_scope: str = "",
    exts: Any = None,
    globs: Any = None,
    no_ignore: bool = False,
    max_results: int = 200,
    max_filesize: str = "10M",
    timeout_ms: int = 20000,
) -> dict:
    started = time.perf_counter()
    warnings: list[str] = []
    if not isinstance(pattern, str) or not pattern:
        raise ComposeError("pattern 不能为空：请提供要搜索的文本或正则表达式。", kind="bad_argument")
    limit = _clamp_int(max_results, 1, MAX_GREP_RESULTS, 200, warnings, "max_results")
    timeout = _clamp_int(timeout_ms, 100, 300000, 20000, warnings, "timeout_ms")
    filesize = str(max_filesize or "10M").strip()
    if not re.match(r"^\d+(?:\.\d+)?\s*[KMGT]?B?$", filesize, re.IGNORECASE):
        raise ComposeError(
            f"max_filesize={max_filesize!r} 格式不正确。示例：10M、500K、1G（单位 K/M/G/T，可带 B）。",
            kind="bad_argument",
        )
    ext_list, ext_warnings = _normalize_exts(exts)
    warnings.extend(ext_warnings)
    glob_list = _normalize_globs(globs)

    scope = str(path_scope or "").strip()
    if scope:
        if not os.path.exists(scope):
            raise ComposeError(
                f"path_scope 不存在：{scope}\n请传入已存在的目录（例如 D:\\Projects）。",
                kind="bad_argument",
            )
        roots = [os.path.abspath(scope)]
    else:
        if not str(query or "").strip():
            raise ComposeError(
                "path_scope 为空且 query 也为空，无法确定搜索范围。\n"
                "请传入 path_scope（例如 D:\\Projects\\workspace），"
                "或传入能命中文件的 query 让工具自动推导搜索根。",
                kind="bad_argument",
            )
        roots = _derive_grep_roots(client, query, warnings)

    rg_exe = _config_value(client, "rg_exe", "EV_RG_EXE", None, lambda value: str(value) if value else None)
    try:
        result = rg.search(
            pattern,
            roots,
            fixed_strings=bool(fixed_strings),
            case_sensitive=bool(case_sensitive),
            globs=glob_list,
            exts=ext_list,
            no_ignore=bool(no_ignore),
            max_filesize=filesize,
            max_results=limit,
            timeout_ms=timeout,
            rg_exe=rg_exe,
        )
    except rg.RgError as exc:
        raise ComposeError(str(exc), kind=exc.kind, details=exc.details) from exc

    warnings.extend(result.get("warnings") or [])
    stats = dict(result.get("stats") or {})
    stats["timeout_ms"] = timeout
    stats["max_results"] = limit
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        "pattern": pattern,
        "fixed_strings": bool(fixed_strings),
        "case_sensitive": bool(case_sensitive),
        "query": query,
        "path_scope": path_scope or None,
        "roots": roots,
        "matches": result.get("matches") or [],
        "stats": stats,
        "truncated": bool(stats.get("truncated")),
        "warnings": warnings,
        "elapsed_ms": elapsed_ms,
    }


# ----------------------------------------------------------------------
# everything_batch_stat
# ----------------------------------------------------------------------
def _stat_mode(mode: int) -> str:
    return f"{mode & 0o7777:04o}"


def _stat_item(path: str, *, include_hash: bool, hash_algo: str, budget: list[int], warnings: list[str]) -> dict:
    item: dict[str, Any] = {
        "path": path,
        "exists": False,
        "type": None,
        "size": None,
        "date_modified": None,
        "date_created": None,
        "mode": None,
        "is_link": False,
        "hash": None,
        "error": None,
    }
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        item["error"] = "路径不存在"
        return item
    except PermissionError:
        item["error"] = "无权访问该路径"
        return item
    except OSError as exc:
        item["error"] = f"读取文件状态失败：{exc}"
        return item

    item["exists"] = True
    is_link = stat_mod.S_ISLNK(info.st_mode) or os.path.islink(path)
    item["is_link"] = bool(is_link)
    if is_link:
        item["type"] = "link"
    elif stat_mod.S_ISDIR(info.st_mode):
        item["type"] = "folder"
    elif stat_mod.S_ISREG(info.st_mode):
        item["type"] = "file"
    else:
        item["type"] = "other"
    item["size"] = int(info.st_size)
    item["date_modified"] = _to_iso(datetime.fromtimestamp(info.st_mtime, tz=timezone.utc))
    created = getattr(info, "st_birthtime", None)
    if created is None:
        created = info.st_ctime
    try:
        item["date_created"] = _to_iso(datetime.fromtimestamp(created, tz=timezone.utc))
    except (OverflowError, OSError, ValueError):
        item["date_created"] = None
    item["mode"] = _stat_mode(info.st_mode)

    if include_hash and item["type"] == "file" and not is_link:
        size = int(info.st_size)
        if size > budget[0]:
            item["hash_skipped"] = f"文件大小 {size} 超过剩余哈希预算 {budget[0]}"
            warnings.append(f"{path}：{item['hash_skipped']}，未计算哈希。")
        else:
            try:
                item["hash"] = _hash_file(path, hash_algo)
                budget[0] -= size
            except OSError as exc:
                item["error"] = f"计算哈希失败：{exc}"
                warnings.append(f"{path}：计算哈希失败（{exc}），已跳过。")
    return item


def batch_stat_impl(
    client: Any,
    *,
    paths: Any,
    include_hash: bool = False,
    hash_algo: str = "md5",
    max_hash_bytes: int = DEFAULT_HASH_MAX_BYTES,
) -> dict:
    started = time.perf_counter()
    warnings: list[str] = []
    path_list = _normalize_paths(paths)
    if not path_list:
        raise ComposeError(
            "paths 不能为空：请传入 1~200 个文件/目录路径（绝对路径）。",
            kind="bad_argument",
        )
    deduped: list[str] = []
    seen: set[str] = set()
    for raw in path_list:
        absolute = os.path.abspath(os.path.expanduser(str(raw).strip()))
        key = os.path.normcase(absolute)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(absolute)
    if len(deduped) > MAX_BATCH_PATHS:
        warnings.append(f"paths 数量 {len(deduped)} 超过上限 {MAX_BATCH_PATHS}，已截断。")
        deduped = deduped[:MAX_BATCH_PATHS]
    algo = str(hash_algo or "md5").strip().lower()
    if algo not in HASH_ALGOS:
        raise ComposeError(
            f"不支持的 hash_algo={hash_algo!r}。可用值：{', '.join(HASH_ALGOS)}。",
            kind="bad_argument",
        )
    config_hash_max = _config_value(
        client, "hash_max_bytes", "EV_HASH_MAX_BYTES", DEFAULT_HASH_MAX_BYTES, _to_int
    )
    budget_bytes = _clamp_int(
        max_hash_bytes if max_hash_bytes is not None else config_hash_max,
        0,
        10**15,
        config_hash_max,
        warnings,
        "max_hash_bytes",
    )
    budget = [budget_bytes]
    items = [
        _stat_item(
            path,
            include_hash=bool(include_hash),
            hash_algo=algo,
            budget=budget,
            warnings=warnings,
        )
        for path in deduped
    ]
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    exists_count = sum(1 for item in items if item["exists"])
    return {
        "items": items,
        "count": len(items),
        "exists_count": exists_count,
        "missing_count": len(items) - exists_count,
        "include_hash": bool(include_hash),
        "hash_algo": algo if include_hash else None,
        "max_hash_bytes": budget_bytes,
        "stats": {
            "elapsed_ms": elapsed_ms,
            "hashed": sum(1 for item in items if item.get("hash")),
            "bytes_remaining": budget[0],
        },
        "warnings": warnings,
        "elapsed_ms": elapsed_ms,
    }


# ----------------------------------------------------------------------
# everything_changes
# ----------------------------------------------------------------------
_RELATIVE_RE = re.compile(
    r"^(?:last\s*)?(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days|w|week|weeks)$"
)


def _parse_since(value: str) -> dict:
    """把 since 归一化为**绝对** dm: 过滤 + 精确时间窗（SPEC：1.4 不支持 dm:lastNh）。

    返回 ``{spec, since, dm, precise, note}``：``dm`` 只使用 ``dm:>=YYYY-MM-DD``
    （本地日期）；小时/分钟级精度由 changes_impl 客户端二次过滤。
    """
    raw = str(value or "").strip()
    if not raw:
        raise ComposeError(
            "since 不能为空。可用值：24h、7d、30m、today、last7days，"
            "或 ISO 时间（2026-10-17、2026-10-17T08:00:00+08:00）。",
            kind="bad_argument",
        )
    lowered = raw.lower().replace("_", " ").strip()
    now_local = datetime.now().astimezone()
    now_utc = now_local.astimezone(timezone.utc)
    if lowered in ("today", "今天"):
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        local_date = start_local.date()
        return {
            "spec": raw,
            "since": start_local.astimezone(timezone.utc),
            "dm": f"dm:>={local_date:%Y-%m-%d}",
            "precise": True,
            "note": "已把 today 归一化为绝对日期过滤 dm:>=YYYY-MM-DD。",
        }

    relative = _RELATIVE_RE.match(lowered)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2)
        try:
            if unit.startswith("m"):
                delta = timedelta(minutes=amount)
            elif unit.startswith("h"):
                delta = timedelta(hours=amount)
            elif unit.startswith("d"):
                delta = timedelta(days=amount)
            else:
                delta = timedelta(weeks=amount)
            cutoff = now_utc - delta
        except OverflowError as exc:
            raise ComposeError(f"since={value!r} 的时间跨度过大，无法计算。", kind="bad_argument") from exc
        local_date = cutoff.astimezone().date()
        note = (
            "Everything 1.4 不支持 dm:lastNh/dm:last7days 等相对语法（会返回 0 条），"
            "已归一化为绝对过滤 dm:>=YYYY-MM-DD（日期级），"
            "具体时间窗由客户端二次过滤；total 为日期级候选数。"
        )
        return {
            "spec": raw,
            "since": cutoff,
            "dm": f"dm:>={local_date:%Y-%m-%d}",
            "precise": True,
            "note": note,
        }

    iso_text = raw.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        moment = datetime.fromisoformat(iso_text)
    except ValueError as exc:
        raise ComposeError(
            f"无法解析 since={value!r}。可用值示例：today、24h、7d、30m、last7days、"
            "2026-10-17、2026-10-17T08:00:00+08:00。",
            kind="bad_argument",
        ) from exc
    if moment.tzinfo is None:
        moment = moment.astimezone()
    precise = bool(re.search(r"[T ]\d{1,2}:\d{2}", raw))
    local_date = moment.astimezone().date()
    return {
        "spec": raw,
        "since": moment.astimezone(timezone.utc),
        "dm": f"dm:>={local_date:%Y-%m-%d}",
        "precise": precise,
        "note": (
            "指定了具体时间：已用绝对日期过滤 dm:>=YYYY-MM-DD + 客户端精确时间过滤。"
            if precise
            else None
        ),
    }


def changes_impl(
    client: Any,
    *,
    since: str = "24h",
    path_scope: str = "",
    exts: Any = None,
    max_results: int = 200,
    sort: str = "date_modified",
    ascending: bool = False,
) -> dict:
    started = time.perf_counter()
    warnings: list[str] = []
    until = datetime.now(timezone.utc)
    parsed = _parse_since(since)
    if parsed.get("note"):
        warnings.append(parsed["note"])
    ext_list, ext_warnings = _normalize_exts(exts)
    warnings.extend(ext_warnings)
    sort_key = str(sort or "date_modified").strip().lower()
    allowed_sorts = {
        "date_modified": "date_modified",
        "modified": "date_modified",
        "name": "name",
        "path": "path",
        "size": "size",
    }
    if sort_key not in allowed_sorts:
        raise ComposeError(
            f"不支持的 sort={sort!r}。可用值：date_modified、name、path、size。",
            kind="bad_argument",
        )
    limit = _clamp_int(max_results, 1, MAX_CHANGES_RESULTS, 200, warnings, "max_results")

    query_parts = [parsed["dm"]]
    if ext_list:
        query_parts.append("ext:" + ";".join(ext_list))
    query = " ".join(query_parts)

    data, degrade = _search_once(
        client,
        query,
        max_results=limit,
        path_scope=path_scope,
        file_only=True,
        sort=allowed_sorts[sort_key],
        ascending=bool(ascending),
        fields=("name", "path", "size", "date_modified", "type", "extension"),
    )
    warnings.extend(degrade)
    total = _as_int(data.get("total")) or 0
    raw_items = [item for item in (data.get("results") or []) if isinstance(item, dict)]
    cutoff = parsed["since"]
    items: list[dict] = []
    unknown_dates = 0
    out_of_window = 0
    for item in raw_items:
        moment = _parse_iso(item.get("date_modified"))
        if moment is None:
            unknown_dates += 1
            continue
        if moment < cutoff or moment > until:
            out_of_window += 1
            continue
        items.append(item)
    if unknown_dates:
        warnings.append(f"有 {unknown_dates} 条结果缺少可解析的 date_modified，已排除。")
    if out_of_window:
        warnings.append(
            f"有 {out_of_window} 条结果落在精确时间窗之外（日期级 dm: 候选的边界），已排除。"
        )

    truncated = bool(total > len(items)) or len(items) >= limit
    if truncated:
        warnings.append(f"结果可能被截断：total={total}，max_results={limit}。")
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    return {
        "since": since,
        "query": query,
        "effective_query": query,
        "path_scope": path_scope or None,
        "items": items,
        "count": len(items),
        "total": total,
        "truncated": truncated,
        "sort": allowed_sorts[sort_key],
        "ascending": bool(ascending),
        "window": {
            "since": _to_iso(cutoff),
            "until": _to_iso(until),
            "spec": parsed["spec"],
            "dm_expression": parsed["dm"],
            "precise": parsed["precise"],
        },
        "stats": {"elapsed_ms": elapsed_ms, "filtered_out": max(0, len(raw_items) - len(items))},
        "warnings": warnings,
        "elapsed_ms": elapsed_ms,
    }


# ----------------------------------------------------------------------
# everything_reveal
# ----------------------------------------------------------------------
def _read_allow_side_effects(client: Any) -> bool:
    """每次调用时解析 EV_ALLOW_SIDE_EFFECTS。

    优先级：**当前进程环境变量** > client.config.allow_side_effects > 默认 True。
    这样同一进程内构造 client 之后再改环境变量也能立即生效（Lead 联调要求）。
    """
    explicit = _env_text("EV_ALLOW_SIDE_EFFECTS")
    if explicit is not None:
        return _to_bool(explicit)
    config = getattr(client, "config", None)
    if config is not None:
        raw = config.get("allow_side_effects") if isinstance(config, dict) else getattr(
            config, "allow_side_effects", None
        )
        if raw is not None:
            return _to_bool(raw)
    return True


def reveal_impl(client: Any, *, path: str) -> dict:
    warnings: list[str] = []
    target = str(path or "").strip().strip('"')
    allow = _read_allow_side_effects(client)
    if not allow:
        # 明确拒绝：不解析路径、不启动 explorer，返回可操作提示。
        return {
            "ok": False,
            "opened": False,
            "mode": None,
            "path": target,
            "error": (
                "已拒绝 everything_reveal：当前 EV_ALLOW_SIDE_EFFECTS=0（只读模式）。\n"
                "该工具会打开 Windows 资源管理器（explorer /select），属于副作用操作，"
                "本次没有启动任何外部程序。"
            ),
            "hint": (
                "如需使用：设置环境变量 EV_ALLOW_SIDE_EFFECTS=1（或移除该变量）后重试；"
                "该开关在每次调用时重新读取，无需重启 MCP server。"
            ),
            "side_effects_enabled": False,
            "warnings": warnings,
        }
    if not target:
        raise ComposeError("path 不能为空：请传入要定位的文件/目录绝对路径。", kind="bad_argument")
    target = os.path.abspath(os.path.expanduser(target))
    if not os.path.exists(target):
        raise ComposeError(
            f"路径不存在：{target}\n请先用 everything_search 确认路径再调用 everything_reveal。",
            kind="not_found",
        )
    windows_dir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    explorer = os.path.join(windows_dir, "explorer.exe")
    if not os.path.isfile(explorer):
        explorer = "explorer.exe"
    if os.path.isdir(target):
        mode = "open"
        args = [explorer, target]
    else:
        mode = "select"
        args = [explorer, f"/select,{target}"]
    creationflags = 0x00000008 if os.name == "nt" else 0  # DETACHED_PROCESS
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise ComposeError(
            f"打开资源管理器失败：{exc}\n目标：{target}\n"
            "请确认 explorer.exe 可用；这不是文件系统写操作。",
            kind="error",
        ) from exc
    return {
        "ok": True,
        "opened": True,
        "mode": mode,
        "path": target,
        "explorer": args[0],
        "pid": process.pid,
        "warnings": warnings,
    }


# ----------------------------------------------------------------------
# MCP 注册
# ----------------------------------------------------------------------
SUMMARY_DESCRIPTION = (
    "对 Everything 命中结果做分页聚合统计（只读）：按扩展名/目录/类型/大小区间/修改月份/父目录分组，"
    "返回每组 count 与字节数、以及命中总数与扫描上限。默认只聚合文件（保证 size 有意义，"
    "include_folders=true 时额外聚合目录）。适合回答“这类文件有多少、占多大、在哪些目录”。"
)
DUPLICATES_DESCRIPTION = (
    "查找重复文件（只读，默认返回 <4KB）：mode=name（同名，大小写不敏感）、name_size（同名且同大小）、"
    "size（同大小）、hash（对同大小候选组做哈希确认）。有 max_files / max_hash_bytes 硬上限；"
    "每组 paths 默认只回前 2 条（max_paths_per_group 可调到 50，完整数量看 path_count），"
    "默认最多 10 组（max_groups 可调到 50），绝不删除/修改任何文件。"
)
GREP_DESCRIPTION = (
    "在真实文件内容中搜索（只读，ripgrep）：返回匹配的 path/line/column/text。"
    "可用 Everything query 自动推导搜索根，或用 path_scope 指定目录；支持固定字符串、大小写、"
    "扩展名/glob 过滤、--no-ignore、单文件大小上限、整体超时与结果上限。"
)
BATCH_STAT_DESCRIPTION = (
    "批量读取路径元数据（只读，最多 200 条）：exists/type/size/date_modified/date_created/mode/"
    "is_link，可选哈希（受 max_hash_bytes 预算限制）。不做任何写操作。"
)
CHANGES_DESCRIPTION = (
    "列出最近改动文件（只读）：since 支持 today、24h、7d、30m、last7days 或 ISO 时间；"
    "用 Everything 的 dm: 日期过滤 + 客户端时间窗二次过滤，返回文件列表与窗口信息。"
)
REVEAL_DESCRIPTION = (
    "在 Windows 资源管理器中定位/打开文件（唯一有副作用的组合工具）：文件用 explorer /select，"
    "目录直接打开。EV_ALLOW_SIDE_EFFECTS=0 时返回 ok=false + 可操作提示（不启动 explorer）；"
    "每次调用重新读取该环境变量。不做删除/移动/复制/重命名。"
)


def register(mcp: Any, get_client: Callable[[], Any]) -> list[str]:
    """把 6 个组合工具注册到给定的 FastMCP 实例上，返回已注册工具名列表。

    调用方（server.py）：
        from .compose_tools import register as register_compose
        register_compose(_mcp, _get_client)

    重复调用是幂等的（通过实例属性标记），import 本模块不会注册任何东西。
    """
    if getattr(mcp, "_everything_compose_registered", False):
        return []
    registered: list[str] = []

    @mcp.tool(name="everything_summary", description=SUMMARY_DESCRIPTION)
    def everything_summary(
        query: str = "",
        path_scope: str = "",
        group_by: str = "ext",
        dir_depth: int = 2,
        top_n: int = 20,
        max_scan: int = 50000,
        include_folders: bool = False,
    ) -> dict:
        """聚合统计 Everything 命中结果（只读）。

        Args:
            query: Everything 查询语法（如 `ext:py`、`path:"D:\\Projects"`）。
            path_scope: 限定路径范围（含子目录）。
            group_by: 分组方式：ext / dir / type / size_bucket / month / parent。
            dir_depth: group_by=dir 时保留的目录层级（1~12）。
            top_n: 返回的组数上限（1~200）。
            max_scan: 最多扫描多少条文件命中（1~EV_SCAN_CAP，默认 50000）。
            include_folders: 是否额外聚合目录（默认只统计文件，size 才有意义；
                目录 size 恒为 0，且 Everything HTTP 空查询存在“目录优先”偏差）。
        """
        return summary_impl(
            get_client(),
            query=query,
            path_scope=path_scope,
            group_by=group_by,
            dir_depth=dir_depth,
            top_n=top_n,
            max_scan=max_scan,
            include_folders=include_folders,
        )

    registered.append("everything_summary")

    @mcp.tool(name="everything_duplicates", description=DUPLICATES_DESCRIPTION)
    def everything_duplicates(
        mode: str = "name",
        query: str = "",
        path_scope: str = "",
        min_size: int = 0,
        max_groups: int = 10,
        max_files: int = 5000,
        hash_algo: str = "md5",
        max_hash_bytes: int = DEFAULT_HASH_MAX_BYTES,
        max_paths_per_group: int = 2,
        max_response_bytes: int = DEFAULT_DUPS_RESPONSE_BYTES,
    ) -> dict:
        """查找重复文件（只读；默认输出体积 <4KB）。

        Args:
            mode: name / name_size / size / hash。
            query: Everything 查询语法，默认扫全部文件。
            path_scope: 限定路径范围（含子目录）。
            min_size: 只考虑不小于该字节数的文件。
            max_groups: 返回的重复组上限（默认 10，上限 50）。
            max_files: 最多扫描/哈希的文件数。
            hash_algo: hash 模式算法：md5/sha1/sha256/sha512/blake2b。
            max_hash_bytes: 哈希读取的总字节预算（默认 256MB）。
            max_paths_per_group: 每组 paths 最多回传几条（默认 2，上限 50）；
                完整数量见 path_count，截断时 paths_truncated=true。
            max_response_bytes: 响应体积预算（默认 4096；0 = 不限制）。
        """
        return duplicates_impl(
            get_client(),
            mode=mode,
            query=query,
            path_scope=path_scope,
            min_size=min_size,
            max_groups=max_groups,
            max_files=max_files,
            hash_algo=hash_algo,
            max_hash_bytes=max_hash_bytes,
            max_paths_per_group=max_paths_per_group,
            max_response_bytes=max_response_bytes,
        )

    registered.append("everything_duplicates")

    @mcp.tool(name="everything_grep", description=GREP_DESCRIPTION)
    def everything_grep(
        pattern: str,
        fixed_strings: bool = False,
        case_sensitive: bool = False,
        query: str = "",
        path_scope: str = "",
        exts: list[str] | None = None,
        globs: list[str] | None = None,
        no_ignore: bool = False,
        max_results: int = 200,
        max_filesize: str = "10M",
        timeout_ms: int = 20000,
    ) -> dict:
        """用 ripgrep 在文件内容中搜索（只读）。

        Args:
            pattern: 要搜索的文本或正则。
            fixed_strings: True 时按字面文本匹配（rg -F）。
            case_sensitive: 是否区分大小写（默认不区分，rg -i）。
            query: Everything 查询；path_scope 为空时用它推导搜索根。
            path_scope: 搜索根目录（推荐显式传入）。
            exts: 扩展名白名单，如 ["py","js"]。
            globs: rg --glob 模式，如 ["!vendor/**"]。
            no_ignore: True 时不遵守 .gitignore（rg --no-ignore）。
            max_results: 全局匹配上限（1~1000）。
            max_filesize: 单文件大小上限（如 10M）。
            timeout_ms: 整体超时（毫秒）。
        """
        return grep_impl(
            get_client(),
            pattern=pattern,
            fixed_strings=fixed_strings,
            case_sensitive=case_sensitive,
            query=query,
            path_scope=path_scope,
            exts=exts,
            globs=globs,
            no_ignore=no_ignore,
            max_results=max_results,
            max_filesize=max_filesize,
            timeout_ms=timeout_ms,
        )

    registered.append("everything_grep")

    @mcp.tool(name="everything_batch_stat", description=BATCH_STAT_DESCRIPTION)
    def everything_batch_stat(
        paths: list[str],
        include_hash: bool = False,
        hash_algo: str = "md5",
        max_hash_bytes: int = DEFAULT_HASH_MAX_BYTES,
    ) -> dict:
        """批量读取路径元数据（只读，最多 200 条）。

        Args:
            paths: 文件/目录绝对路径列表（最多 200 条）。
            include_hash: 是否计算文件哈希（只读）。
            hash_algo: md5/sha1/sha256/sha512/blake2b。
            max_hash_bytes: 哈希读取的总字节预算。
        """
        return batch_stat_impl(
            get_client(),
            paths=paths,
            include_hash=include_hash,
            hash_algo=hash_algo,
            max_hash_bytes=max_hash_bytes,
        )

    registered.append("everything_batch_stat")

    @mcp.tool(name="everything_changes", description=CHANGES_DESCRIPTION)
    def everything_changes(
        since: str = "24h",
        path_scope: str = "",
        exts: list[str] | None = None,
        max_results: int = 200,
        sort: str = "date_modified",
        ascending: bool = False,
    ) -> dict:
        """列出最近改动的文件（只读）。

        Args:
            since: today / 24h / 7d / 30m / last7days / ISO 时间（2026-10-17T08:00:00+08:00）。
            path_scope: 限定路径范围。
            exts: 扩展名白名单，如 ["py","js"]。
            max_results: 返回条数上限（1~1000）。
            sort: date_modified / name / path / size。
            ascending: 是否升序（默认按修改时间降序）。
        """
        return changes_impl(
            get_client(),
            since=since,
            path_scope=path_scope,
            exts=exts,
            max_results=max_results,
            sort=sort,
            ascending=ascending,
        )

    registered.append("everything_changes")

    @mcp.tool(name="everything_reveal", description=REVEAL_DESCRIPTION)
    def everything_reveal(path: str) -> dict:
        """在资源管理器中定位/打开文件或目录（受 EV_ALLOW_SIDE_EFFECTS 控制）。

        Args:
            path: 要定位的文件/目录绝对路径。
        """
        return reveal_impl(get_client(), path=path)

    registered.append("everything_reveal")

    setattr(mcp, "_everything_compose_registered", True)
    logger.info("已注册组合工具：%s", ", ".join(registered))
    return registered
