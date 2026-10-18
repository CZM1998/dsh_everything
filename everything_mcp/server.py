"""FastMCP stdio 服务器：Everything 查询层 + 组合工具挂载点 + MCP 资源。

查询类工具（本文件，task-3；在 DSH 中显示为 ``mcp__everything__<name>``）：
- ``everything_search``   搜索（分页/排序/fields/count_only/path_scope/类型过滤）；
- ``everything_count``    只返回命中数量；
- ``everything_status``   运行状态、HTTP/索引/DB/卷/ini 开关/rg/预设/MCP 能力诊断；
- ``everything_help``     能力矩阵 + 工具清单 + 示例（自解释入口）；
- ``everything_presets``  列出/运行内置或自定义预设；
- ``everything_export``   导出 csv/jsonl/efu（仅限 EV_EXPORT_ROOT 内）；
- ``everything_download`` 经 Everything HTTP Server 下载文件到 EV_DOWNLOAD_ROOT。

MCP 资源：
- ``everything://help/syntax`` 能力矩阵 Markdown；
- ``everything://status``      当前状态 JSON；
- ``everything://presets``     预设清单 JSON。

组合工具（``everything_summary`` / ``everything_duplicates`` / ``everything_grep``
/ ``everything_batch_stat`` / ``everything_changes`` / ``everything_reveal``）由
``compose_tools.register(mcp, get_client)`` 注册；若该模块缺失或注册失败，服务
**仍然正常启动**，只是不出现这些工具（日志告警）。

启动方式：
    python everything-mcp/server.py
    python -m everything_mcp            # 在 everything-mcp/ 目录下
"""

from __future__ import annotations

import json
import logging
import os
import sys

from mcp.server.fastmcp import FastMCP

from . import __version__
from . import help as help_module
from . import presets as presets_module
from .client import EverythingClient, EverythingError
from .config import Config

logger = logging.getLogger("everything_mcp.server")

PREREQUISITE = (
    "前提条件：Windows + voidtools Everything（1.4/1.5）已安装；Everything 内置 HTTP Server "
    "已启用并绑定到 127.0.0.1:8090（Everything.ini 中 http_server_enabled=1、"
    "http_server_port=8090、http_server_bindings=127.0.0.1；绑定项只能写 IP，不能写 IP:端口）。"
    "Everything 未运行时本服务器会自动拉起（EV_AUTO_START=1，默认开启）。"
    "遇到问题先调用 everything_status 获取诊断与修复提示。"
)

SEARCH_DESCRIPTION = (
    "在 Everything 索引中搜索文件/目录，毫秒级返回（无需递归扫描磁盘）。"
    "支持 Everything 全部搜索语法（ext:、path:、size:、dm: 等），regex=True 时 query 按正则解释。"
    "支持分页（offset/next_offset）、排序（name/path/size/date_modified）、"
    "path_scope 路径范围、folder_only/file_only、fields 字段裁剪、count_only 只取总数。"
    "单次最多返回 max_results 条（默认 100，上限 EV_MAX_RESULTS_CAP=1000）。\n"
    + PREREQUISITE
)

COUNT_DESCRIPTION = (
    "只返回 Everything 查询的命中总数（不返回文件列表），用于快速判断文件/目录是否存在。"
    "比 everything_search 更省 token。支持 regex、path_scope、folder_only/file_only 等过滤。\n"
    + PREREQUISITE
)

STATUS_DESCRIPTION = (
    "检查 Everything 是否在运行、内置 HTTP Server 是否可达、Everything 版本、索引条目数、"
    "索引 DB 路径与大小、磁盘卷、ini 关键索引开关（date_created/date_accessed/attributes/"
    "folder_size）、ripgrep 可用性、预设数量、auto_start 状态与 HTTP 能力矩阵摘要；"
    "并给出可操作修复提示。排查搜索失败时优先调用。\n" + PREREQUISITE
)

HELP_DESCRIPTION = (
    "返回 Everything 能力的自解释入口：支持/不支持的搜索语法、慢查询（dc:/da:/attrib: 的 ini 依赖）、"
    "HTTP JSON 参数矩阵、本服务器全部工具清单与示例、使用提示，并附 Markdown 文本。"
    "不清楚某语法/参数是否可用时先调用它，不要猜。\n" + PREREQUISITE
)

PRESETS_DESCRIPTION = (
    "列出或运行预设查询。action=list 返回内置与自定义预设（EV_PRESETS_FILE 可覆盖/追加）；"
    "action=run 按预设执行搜索，可覆盖 max_results/offset/path_scope。"
    "内置预设：recent_modified_7d、today_changed、large_files_100mb、screenshots_7d、"
    "archives_100mb、git_repos、node_modules、downloads_today。\n" + PREREQUISITE
)

EXPORT_DESCRIPTION = (
    "把 Everything 查询结果分页导出为文件：format=csv（UTF-8 BOM，Excel 可直接打开）/ "
    "jsonl（每行一个 JSON 对象）/ efu（Everything 文件列表）。"
    "out_csv（或 out_path）必须是工作区内的相对路径（相对于 EV_EXPORT_ROOT），"
    "或位于 EV_EXPORT_ROOT 之下的绝对路径；超出范围会被拒绝。"
    "最多导出 max_results 行（默认 5000，上限 10000），offset 支持从任意位置开始。\n"
    + PREREQUISITE
)

DOWNLOAD_DESCRIPTION = (
    "把 Everything 搜索得到的文件通过 Everything 内置 HTTP Server 下载到本机工作区。"
    "path 必须是绝对文件路径（来自 everything_search 结果）；dest 省略时保存到 "
    "EV_DOWNLOAD_ROOT（默认工作区）下同名文件（重名自动加序号）。"
    "只允许写 EV_DOWNLOAD_ROOT 内；超过 max_bytes（默认 2GiB）会中止并清理半成品。\n"
    + PREREQUISITE
)

_mcp = FastMCP(
    "everything",
    instructions=(
        "Everything 文件搜索服务器：通过本机 voidtools Everything 索引做毫秒级文件名/路径"
        "搜索，支持正则与 Everything 原生语法（ext:/path:/size:/dm: 等）。"
        "优先用它替代 find/Get-ChildItem 递归扫描。"
        "不确定语法是否可用时先调用 everything_help；排查故障用 everything_status。"
        "每个工具描述都附带 HTTP Server 前提条件。\n" + PREREQUISITE
    ),
)

_client: EverythingClient | None = None


def _get_client() -> EverythingClient:
    global _client
    if _client is None:
        _client = EverythingClient(Config.from_env())
    return _client


def _call(action, **kwargs):
    """统一异常转换：把 EverythingError 变成 MCP 可见的错误文本。"""
    try:
        return action(**kwargs)
    except EverythingError as exc:
        raise RuntimeError(str(exc)) from exc


# ----------------------------------------------------------------------
# MCP 资源（读取失败不得影响工具）
# ----------------------------------------------------------------------
@_mcp.resource(
    "everything://help/syntax",
    description="Everything 搜索语法能力矩阵（支持/不支持/慢查询/HTTP 参数）",
    mime_type="text/markdown",
)
def resource_help_syntax() -> str:
    """能力矩阵 Markdown；读取出错时返回错误说明文本而不是抛异常。"""
    try:
        return help_module.render_syntax_markdown()
    except Exception as exc:  # noqa: BLE001 - 资源读取失败不得影响工具
        logger.warning("资源 everything://help/syntax 读取失败：%s", exc)
        return f"# 读取失败\n\n`everything://help/syntax` 暂时不可用：{exc}"


@_mcp.resource(
    "everything://status",
    description="Everything / HTTP / 索引 / 配置 当前状态 JSON",
    mime_type="application/json",
)
def resource_status() -> str:
    """当前状态 JSON；读取出错时返回 {"ok": false, "error": ...}。"""
    try:
        return json.dumps(_get_client().status(), ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.warning("资源 everything://status 读取失败：%s", exc)
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


@_mcp.resource(
    "everything://presets",
    description="预设查询清单 JSON（内置 + EV_PRESETS_FILE）",
    mime_type="application/json",
)
def resource_presets() -> str:
    """预设清单 JSON；读取出错时返回 {"ok": false, "error": ...}。"""
    try:
        config = _get_client().config
        return json.dumps(presets_module.describe(config), ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        logger.warning("资源 everything://presets 读取失败：%s", exc)
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


# ----------------------------------------------------------------------
# 查询类 MCP 工具
# ----------------------------------------------------------------------
@_mcp.tool(description=SEARCH_DESCRIPTION)
def everything_search(
    query: str,
    regex: bool = False,
    case_sensitive: bool = False,
    whole_word: bool = False,
    match_path: bool = False,
    path_scope: str = "",
    folder_only: bool = False,
    file_only: bool = False,
    sort: str = "",
    ascending: bool | None = None,
    offset: int = 0,
    max_results: int = 100,
    fields: list[str] | None = None,
    count_only: bool = False,
    # v1 兼容参数（已废弃）
    columns: list[str] | None = None,
    path_filter: str = "",
) -> dict:
    """在 Everything 索引中搜索文件/目录。

    Args:
        query: 查询字符串。regex=False 时用 Everything 语法（如 `*.exe`、`blender ext:exe`、
            `path:D:\\software`）；regex=True 时按正则解释（如 `blender.*\\.exe`）。
        regex: 是否把 query 作为正则表达式（对应 Everything “启用正则”）。
        case_sensitive: 区分大小写。
        whole_word: 全字匹配。
        match_path: 同时匹配完整路径（而不仅是文件名）。
        path_scope: 只搜索该路径（含子目录）下；组合 `path:"<scope>"` + 客户端前缀双保险。
            注意：regex=True 时只能客户端近似过滤（返回会带 warnings/approx）。
        folder_only: 只返回目录（组合 `folder:`）。
        file_only: 只返回文件（组合 `file:`）。
        sort: 排序字段，仅支持 name/path/size/date_modified（其他值告警并回退 name）。
        ascending: True=升序，False=降序，None=Everything 默认。
        offset: 分页起点（0 基）；用返回的 next_offset 取下一页。
        max_results: 返回条数上限，默认 100，最大 EV_MAX_RESULTS_CAP=1000。
        fields: 返回字段子集，可选 type/name/path/size/date_modified/extension；
            未请求 path/size/date_modified 时不会向 Everything 请求对应列（省 token）。
        count_only: 只返回命中总数（等价 everything_count，但保留 query 元信息）。
        columns: 已废弃，v1 兼容；请改用 fields。
        path_filter: 已废弃，v1 兼容；等价 path_scope。

    Returns:
        含 query/effective_query/total/returned/next_offset/truncated/results/warnings 等。
    """
    return _call(
        _get_client().search,
        query=query,
        regex=regex,
        case_sensitive=case_sensitive,
        whole_word=whole_word,
        match_path=match_path,
        path_scope=path_scope,
        folder_only=folder_only,
        file_only=file_only,
        sort=sort,
        ascending=ascending,
        offset=offset,
        max_results=max_results,
        fields=fields,
        count_only=count_only,
        columns=columns,
        path_filter=path_filter,
    )


@_mcp.tool(description=COUNT_DESCRIPTION)
def everything_count(
    query: str,
    regex: bool = False,
    case_sensitive: bool = False,
    whole_word: bool = False,
    match_path: bool = False,
    path_scope: str = "",
    folder_only: bool = False,
    file_only: bool = False,
    path_filter: str = "",
) -> dict:
    """只返回 Everything 查询的命中数量。

    Args:
        query: 查询字符串（Everything 语法或正则，取决于 regex）。
        regex: 是否按正则表达式解释 query。
        case_sensitive: 区分大小写。
        whole_word: 全字匹配。
        match_path: 同时匹配完整路径。
        path_scope: 只统计该路径（含子目录）下的结果（组合 `path:"..."`）。
        folder_only: 只统计目录。file_only: 只统计文件。
        path_filter: 已废弃，v1 兼容；等价 path_scope。
    """
    return _call(
        _get_client().count,
        query=query,
        regex=regex,
        case_sensitive=case_sensitive,
        whole_word=whole_word,
        match_path=match_path,
        path_scope=path_scope,
        folder_only=folder_only,
        file_only=file_only,
        path_filter=path_filter,
    )


@_mcp.tool(description=STATUS_DESCRIPTION)
def everything_status() -> dict:
    """返回 Everything 与 HTTP 通道的完整状态诊断。"""
    return _call(_get_client().status)


@_mcp.tool(description=HELP_DESCRIPTION)
def everything_help() -> dict:
    """返回能力矩阵、工具清单、示例与提示（结构化 + markdown）。"""
    return help_module.build_help()


@_mcp.tool(description=PRESETS_DESCRIPTION)
def everything_presets(
    action: str = "list",
    name: str = "",
    max_results: int = 0,
    offset: int = 0,
    path_scope: str = "",
) -> dict:
    """列出或运行预设查询。

    Args:
        action: "list"（默认）列出预设；"run" 运行 name 指定的预设。
        name: action=run 时的预设名（见 action=list 的返回）。
        max_results: 覆盖预设的返回条数（0 表示用预设默认值）。
        offset: 分页起点。
        path_scope: 覆盖预设的路径范围。
    """
    client = _get_client()
    normalized = str(action or "list").strip().lower()
    try:
        if normalized == "list":
            data = presets_module.describe(client.config)
            return {
                "action": "list",
                "count": data["count"],
                "presets": data["presets"],
                "warnings": data["warnings"],
            }
        if normalized == "run":
            if not str(name or "").strip():
                raise EverythingError(
                    "action=run 时必须提供 name；可先调用 everything_presets(action=\"list\") 查看预设。",
                    kind="bad_argument",
                )
            preset, warnings = presets_module.get_preset(name, client.config)
            if preset is None:
                available = presets_module.available_names(client.config)
                raise EverythingError(
                    f"未知预设：{name!r}。可用预设：{available}",
                    kind="bad_argument",
                )
            kwargs = presets_module.build_search_kwargs(
                preset,
                client.config,
                max_results=max_results if max_results > 0 else None,
                offset=offset,
                path_scope=path_scope,
            )
            result = client.search(preset["query"], **kwargs)
            return {
                "action": "run",
                "name": preset["name"],
                "description": preset.get("description", ""),
                "preset": preset,
                "result": result,
                "warnings": warnings + list(result.get("warnings") or []),
            }
        raise EverythingError(
            f"不支持的 action：{action!r}。可用值：list、run。", kind="bad_argument"
        )
    except EverythingError as exc:
        raise RuntimeError(str(exc)) from exc


@_mcp.tool(description=EXPORT_DESCRIPTION)
def everything_export(
    query: str,
    out_csv: str = "",
    format: str = "csv",
    regex: bool = False,
    case_sensitive: bool = False,
    whole_word: bool = False,
    match_path: bool = False,
    max_results: int = 5000,
    offset: int = 0,
    sort: str = "",
    ascending: bool | None = None,
    columns: list[str] | None = None,
    path_scope: str = "",
    folder_only: bool = False,
    file_only: bool = False,
    out_path: str = "",
    path_filter: str = "",
) -> dict:
    """把 Everything 查询结果导出为 csv/jsonl/efu（仅限工作区内）。

    Args:
        query: 查询字符串。
        out_csv: 输出路径；相对路径基于 EV_EXPORT_ROOT（默认工作区根目录）。
        format: csv（默认，UTF-8 BOM）/ jsonl / efu。
        regex: 是否按正则表达式解释 query。
        case_sensitive: 区分大小写。
        whole_word: 全字匹配。
        match_path: 同时匹配完整路径。
        max_results: 最多导出行数，默认 5000，最大 10000。
        offset: 从第 offset 条开始导出（分页导出用）。
        sort: 排序字段（name/path/size/date_modified）。
        ascending: True=升序，False=降序。
        columns: 导出的列（缺省 name/path/type/size/date_modified/extension）。
        path_scope: 只导出该路径（含子目录）下的结果。
        folder_only/file_only: 只导出目录/文件。
        out_path: out_csv 的别名（任一提供即可）。
        path_filter: 已废弃，v1 兼容；等价 path_scope。
    """
    return _call(
        _get_client().export,
        query=query,
        out_csv=out_csv or None,
        out_path=out_path or None,
        format=format,
        regex=regex,
        case_sensitive=case_sensitive,
        whole_word=whole_word,
        match_path=match_path,
        max_results=max_results,
        offset=offset,
        sort=sort,
        ascending=ascending,
        columns=columns,
        path_scope=path_scope,
        folder_only=folder_only,
        file_only=file_only,
        path_filter=path_filter,
    )


@_mcp.tool(description=DOWNLOAD_DESCRIPTION)
def everything_download(path: str, dest: str = "", max_bytes: int = 0) -> dict:
    """经 Everything HTTP Server 下载文件到工作区内。

    Args:
        path: 要下载的文件绝对路径（通常来自 everything_search 结果）。
        dest: 目标路径；省略时保存到 EV_DOWNLOAD_ROOT 下同名文件（重名自动加序号）。
            相对路径基于 EV_DOWNLOAD_ROOT；绝对路径必须在 EV_DOWNLOAD_ROOT 内。
        max_bytes: 单文件大小上限（字节，默认 2GiB）；超过会中止并清理半成品。
    """
    return _call(_get_client().download, path=path, dest=dest, max_bytes=max_bytes)


# ----------------------------------------------------------------------
# 组合工具注册（task-4；缺失/异常时降级，不影响服务启动）
# ----------------------------------------------------------------------
_compose_status: dict = {"registered": False, "reason": "未尝试注册"}


def register_compose_tools() -> dict:
    """尝试注册 ``compose_tools`` 的组合工具；失败只告警，不抛异常。"""
    global _compose_status
    try:
        from .compose_tools import register as register_compose
    except Exception as exc:  # noqa: BLE001 - 模块可能尚未实现/正在开发
        _compose_status = {"registered": False, "reason": f"compose_tools 不可用：{exc}"}
        logger.info("组合工具未注册（%s）", _compose_status["reason"])
        return _compose_status
    try:
        register_compose(_mcp, _get_client)
    except Exception as exc:  # noqa: BLE001 - 注册失败不得阻止服务启动
        _compose_status = {"registered": False, "reason": f"compose_tools.register 失败：{exc}"}
        logger.warning("组合工具注册失败（%s）", _compose_status["reason"])
        return _compose_status
    _compose_status = {"registered": True, "reason": ""}
    logger.info("已注册组合工具（compose_tools）")
    return _compose_status


# 在 main() 启动前注册，保证 stdio 与内存传输都可见；失败仅告警。
register_compose_tools()


def main() -> None:
    """入口：配置日志（只写 stderr，避免污染 MCP stdio）、自检、启动服务。"""
    level_name = os.environ.get("EV_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    config = Config.from_env()
    client = EverythingClient(config)
    logger.info(
        "everything-mcp %s 启动：HTTP=%s，exe=%s，auto_start=%s，compose=%s",
        __version__,
        config.http_url,
        config.exe,
        config.auto_start,
        _compose_status,
    )
    # 启动自检只探测，不产生副作用；失败不影响服务启动，工具调用会给出修复建议。
    if os.environ.get("EV_SKIP_SELF_CHECK", "").strip() not in {"1", "true", "yes"}:
        try:
            check = client.self_check()
            logger.info("启动自检：%s", check)
        except Exception as exc:  # noqa: BLE001 - 自检失败不应阻止服务启动
            logger.warning("启动自检异常：%s", exc)
    _mcp.run(transport="stdio")


# 供测试/扩展引用
__all__ = [
    "_mcp",
    "_get_client",
    "everything_search",
    "everything_count",
    "everything_status",
    "everything_help",
    "everything_presets",
    "everything_export",
    "everything_download",
    "register_compose_tools",
]
