"""Everything 客户端：通道选择、自动拉起、参数校验与 v2 全部高层操作。

设计要点
--------
1. **HTTP 优先**：查询全部走 Everything 内置 HTTP Server（本机 loopback，
   单次 1~5ms）。IPC（WM_COPYDATA）只用于“Everything 是否在运行”的探测，
   ETP 不启用；原因见 README“查询通道”一节。
2. **自动拉起**：HTTP 不可达且 Everything 未运行时，若 ``EV_AUTO_START``
   未关闭，则用 ``subprocess.Popen`` 以最小化方式启动 Everything（默认
   读用户配置，因此能复用用户索引，毫秒级返回）。
3. **可操作错误**：区分“Everything 未运行”“已运行但 HTTP Server 未启用”
   “查询超时”“返回协议异常”，并给出具体修复步骤。
4. **上限保护**：单次返回最多 ``max_results``（默认 100，且不超过
   ``EV_MAX_RESULTS_CAP``=1000），绝不无限制返回整盘列表。

v2 接口契约（SPEC §2，task-4 依赖）
-----------------------------------
- ``search``：分页/排序/fields/count_only/path_scope/folder_only/file_only，
  返回 ``next_offset`` / ``effective_query`` / ``warnings`` 等；
- ``count`` / ``status`` / ``export``（csv|jsonl|efu）/ ``download`` /
  ``search_pages``（生成器，供 summary/duplicates 分页扫描）。
- **不支持的参数必须进 warnings，不得静默**；sort 白名单外的值回退 ``name``。
"""

from __future__ import annotations

import csv
import ctypes
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Iterator

from . import help as help_module
from . import presets as presets_module
from . import winipc
from .config import Config, find_rg
from .http_channel import (
    EverythingHttpChannel,
    EverythingHttpError,
    HttpNotFound,
    HttpProtocolError,
    HttpTimeout,
    HttpTooLarge,
    HttpUnavailable,
    SORT_ALIASES,
    SUPPORTED_SORT_KEYS,
    iso_to_filetime,
)

logger = logging.getLogger("everything_mcp.client")

#: 支持的输出字段（SPEC §3.1：fields ⊂ 此集合；extension 由 name 派生）
SUPPORTED_FIELDS = ("type", "name", "path", "size", "date_modified", "extension")
DEFAULT_FIELDS = list(SUPPORTED_FIELDS)
#: 1.4 HTTP 不支持的列（v1 兼容参数里可能出现）→ 只告警、不请求
UNSUPPORTED_FIELDS = {"date_created", "date_accessed", "attributes", "run_count"}
FIELD_ALIASES = {
    "modified": "date_modified",
    "mtime": "date_modified",
    "ext": "extension",
    "filename": "name",
    "dir": "path",
    "created": "date_created",
    "accessed": "date_accessed",
}
#: 支持的排序（1.4 HTTP 实测生效）
SUPPORTED_SORTS = set(SUPPORTED_SORT_KEYS)

EXPORT_FORMATS = ("csv", "jsonl", "efu")
EXPORT_SUFFIX = {"csv": ".csv", "jsonl": ".jsonl", "efu": ".efu"}
EXPORT_HARD_CAP = 10000
DEFAULT_PAGE_SIZE = 1000
#: everything_download 默认单文件上限（2 GiB），可用 max_bytes 覆盖
DOWNLOAD_MAX_BYTES = 2 * 1024**3


class EverythingError(RuntimeError):
    """面向 Agent 的可操作错误（消息为中文，包含修复建议）。

    ``kind`` 便于调用方/测试区分错误类别：
    ``not_running`` / ``not_installed`` / ``http_disabled`` / ``start_failed`` /
    ``timeout`` / ``protocol`` / ``bad_argument`` / ``export_outside_workspace`` /
    ``download_outside_root`` / ``not_found`` / ``too_large``。
    """

    def __init__(self, message: str, *, kind: str = "error", details: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.details = details or {}


# ----------------------------------------------------------------------
# 小工具
# ----------------------------------------------------------------------
def _clamp_int(value, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _split_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()]


def _normalize_prefix(scope: str) -> str:
    """路径前缀归一化：统一反斜杠、去掉尾部斜杠、小写（用于 startsWith 比较）。"""
    text = str(scope).strip()
    if not text:
        return ""
    text = text.replace("/", "\\").rstrip("\\")
    return text.lower()


def _scope_for_query(scope: str) -> str:
    """把 path_scope 转成可写进搜索语法的值（去引号、统一反斜杠、去尾部斜杠）。"""
    text = str(scope).strip().replace('"', "").replace("/", "\\")
    return text.rstrip("\\")


def _match_client_filters(item: dict, *, prefix: str, want_type: str | None) -> bool:
    """客户端二次过滤：路径前缀（双保险）+ 类型（regex 模式的近似过滤）。"""
    if want_type and item.get("type") != want_type:
        return False
    if prefix:
        path = str(item.get("path") or "").lower()
        if not path.startswith(prefix):
            return False
    return True


def _project_fields(item: dict, fields: list[str]) -> dict:
    """只保留请求的字段；``name`` / ``type`` 始终保留（Everything 固有字段）。"""
    allowed = set(fields) | {"type", "name"}
    projected: dict[str, Any] = {}
    for key in SUPPORTED_FIELDS:
        if key in allowed and key in item:
            projected[key] = item[key]
    return projected


def _normalize_fields(fields, warnings: list[str]) -> list[str]:
    """校验 fields；不支持的值进 warnings 并忽略。None → 默认全字段。"""
    if fields is None:
        return list(DEFAULT_FIELDS)
    requested = _split_list(fields)
    if not requested:
        warnings.append("fields 为空，结果只包含 name/type 字段。")
        return []
    normalized: list[str] = []
    for raw in requested:
        key = raw.strip().lower().replace("-", "_")
        key = FIELD_ALIASES.get(key, key)
        if key in SUPPORTED_FIELDS:
            if key not in normalized:
                normalized.append(key)
        elif key in UNSUPPORTED_FIELDS:
            warnings.append(
                f"fields 中的 {raw!r} 在 Everything 1.4 HTTP 不可用（date_created/"
                "date_accessed/attributes 均未索引/未返回），已忽略。可用字段："
                f"{list(SUPPORTED_FIELDS)}。"
            )
        else:
            warnings.append(
                f"fields 中的 {raw!r} 不是支持的字段，已忽略。可用字段：{list(SUPPORTED_FIELDS)}。"
            )
    return normalized


def _normalize_sort(sort, warnings: list[str]) -> str:
    """sort 白名单：name/path/size/date_modified；其余告警并回退 ``name``。"""
    if not sort:
        return ""
    raw = str(sort).strip()
    key = SORT_ALIASES.get(raw.lower(), raw.lower())
    if key in SUPPORTED_SORTS:
        return key
    warnings.append(
        f"sort={raw!r} 在 Everything 1.4 HTTP 不受支持（可用：name/path/size/"
        "date_modified；date_created/extension 等是 1.5/未生效特性），已回退按 name 排序。"
    )
    return "name"


def _resolve_ambiguous_bools(folder_only, file_only, warnings: list[str]) -> tuple[bool, bool]:
    folder = bool(folder_only)
    file_ = bool(file_only)
    if folder and file_:
        warnings.append(
            "folder_only 与 file_only 同时为 true 互相矛盾；已保留 file_only，忽略 folder_only。"
        )
        folder = False
    return folder, file_


class EverythingClient:
    """Everything 查询门面。"""

    def __init__(self, config: Config):
        self.config = config
        self.http = EverythingHttpChannel(config.host, config.port, config.http_timeout)
        self._started_pid: int | None = None
        self._started_at: float | None = None
        self._last_error: str | None = None

    # ------------------------------------------------------------------
    # 状态与错误提示
    # ------------------------------------------------------------------
    @property
    def started_by_server(self) -> bool:
        return self._started_pid is not None

    def self_check(self) -> dict:
        """启动时自检：只探测 HTTP，不产生副作用（不自动拉起）。"""
        reachable = self.http.ping()
        instances = winipc.describe_instances()
        if not reachable:
            if instances["running"]:
                logger.warning(
                    "Everything 正在运行，但 HTTP Server 不可达（%s）。"
                    "工具调用会返回修复步骤。",
                    self.config.http_url,
                )
            else:
                logger.warning(
                    "Everything 未运行（%s）；首次查询时将尝试自动拉起（EV_AUTO_START=%s）。",
                    self.config.exe,
                    self.config.auto_start,
                )
        return {
            "http_reachable": reachable,
            "everything_running": instances["running"],
        }

    def _not_running_message(self) -> str:
        return (
            f"Everything 未运行，且无法自动启动（EV_AUTO_START={self.config.auto_start}）。\n"
            f"请手动启动：{self.config.exe}\n"
            "或设置 EV_EVERYTHING_EXE 指向 Everything.exe；"
            "也可设置 EV_AUTO_START=1（默认）让本服务器自动拉起。"
        )

    def _not_installed_message(self) -> str:
        return (
            f"找不到 Everything.exe：{self.config.exe}\n"
            "请安装 voidtools Everything，或设置环境变量 EV_EVERYTHING_EXE 指向正确路径。"
        )

    def _http_disabled_message(self, instances: dict | None = None) -> str:
        lines = [
            f"检测到 Everything 正在运行，但内置 HTTP Server 不可达（{self.config.http_url}）。",
            "请启用 HTTP Server 后重启 Everything：",
            "  1) Everything 菜单：工具 → 选项 → HTTP Server；",
            "  2) 勾选“启用 HTTP Server”，端口设为 "
            f"{self.config.port}，绑定地址只填 {self.config.host}；",
            "  3) 用户配置文件 %APPDATA%\\Everything\\Everything.ini 中应包含：",
            "       http_server_enabled=1",
            f"       http_server_port={self.config.port}",
            f"       http_server_bindings={self.config.host}",
            "     （注意：绑定项只写 IP，不能写成 127.0.0.1:8090，否则 HTTP Server 会静默不启动）",
            "  4) 完全退出并重新启动 Everything；",
            f"  5) 验证：浏览器打开 {self.config.http_url} 能返回搜索结果。",
            "说明：若端口与绑定值与本服务器配置不一致，可用 EV_HTTP_HOST/EV_HTTP_PORT 覆盖。",
        ]
        if instances and instances.get("processes"):
            pids = ", ".join(str(proc["pid"]) for proc in instances["processes"])
            lines.insert(1, f"当前 Everything 进程 PID：{pids}。")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 通道保障 / 自动拉起
    # ------------------------------------------------------------------
    def _ensure_channel(self, *, allow_start: bool = True) -> str:
        """确保 HTTP 通道可用；必要时自动拉起 Everything。返回通道名。"""
        if self.http.ping():
            return "http"
        instances = winipc.describe_instances()
        if instances["running"]:
            raise EverythingError(
                self._http_disabled_message(instances),
                kind="http_disabled",
                details={"instances": instances},
            )
        if not allow_start or not self.config.auto_start:
            raise EverythingError(self._not_running_message(), kind="not_running")
        self._start_everything()
        return "http"

    def _start_everything(self) -> None:
        """以最小化方式启动 Everything（使用用户配置或其 ``-config`` 指定配置）。"""
        exe_path = Path(self.config.exe)
        if not exe_path.is_file():
            raise EverythingError(self._not_installed_message(), kind="not_installed")

        # 生产形态：复用用户默认实例的配置，用 -startup 在后台拉起，不指定
        # -instance/-config（命名实例无法使用 Everything 服务，会弹管理员对话框）。
        args = [str(exe_path)]
        if self.config.config_ini:
            ini_path = Path(self.config.config_ini)
            if not ini_path.is_file():
                raise EverythingError(
                    f"EV_EVERYTHING_CONFIG 指向的配置文件不存在：{ini_path}",
                    kind="start_failed",
                )
            args += ["-config", str(ini_path)]
        args += ["-startup"]

        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 7  # SW_SHOWMINNOACTIVE：最小化启动，不抢焦点
            creationflags = 0x00000008  # DETACHED_PROCESS：不继承 stdio，避免占用 MCP 管道

        logger.info("Everything 未运行，正在自动拉起：%s", args)
        try:
            process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                startupinfo=startupinfo,
                creationflags=creationflags,
                close_fds=True,
            )
        except OSError as exc:
            self._last_error = f"启动失败：{exc}"
            raise EverythingError(
                f"启动 Everything 失败：{exc}\n请手动启动：{self.config.exe}",
                kind="start_failed",
            ) from exc

        self._started_pid = process.pid
        self._started_at = time.time()

        deadline = time.monotonic() + self.config.start_timeout
        while time.monotonic() < deadline:
            if self.http.ping():
                logger.info(
                    "Everything 已就绪（PID=%s，HTTP %s）", process.pid, self.config.http_url
                )
                return
            if process.poll() is not None:
                time.sleep(0.3)
                if self.http.ping():
                    return
                message = (
                    f"启动 Everything 后进程立即退出（退出码 {process.returncode}）。\n"
                    "可能原因：已有 Everything 实例在运行但未启用 HTTP Server，"
                    "或 -config 指定的配置与现有实例冲突。\n"
                    f"请手动启动并检查：{self.config.exe}"
                )
                self._last_error = message
                raise EverythingError(message, kind="start_failed")
            time.sleep(0.25)

        message = (
            f"已启动 Everything（PID={process.pid}），但 {self.config.start_timeout:.0f}s 内 "
            f"HTTP Server 仍未就绪（{self.config.http_url}）。\n"
            "可能原因：\n"
            "  - Everything 里没有启用 HTTP Server（工具 → 选项 → HTTP Server）；\n"
            "  - 端口/绑定地址与 EV_HTTP_PORT/EV_HTTP_HOST 不一致；\n"
            "  - Everything 正在首次构建索引（可稍后重试 everything_status 查看索引条目数）。\n"
            "可用 everything_status 获取详细诊断。"
        )
        self._last_error = message
        raise EverythingError(message, kind="start_failed")

    # ------------------------------------------------------------------
    # HTTP 查询（带一次恢复重试）
    # ------------------------------------------------------------------
    def _convert_http_error(self, exc: EverythingHttpError) -> EverythingError:
        if isinstance(exc, HttpTimeout):
            return EverythingError(
                f"{exc}\n索引可能正在构建或磁盘繁忙；可加大 EV_HTTP_TIMEOUT（当前 "
                f"{self.config.http_timeout:.1f}s）后重试。",
                kind="timeout",
            )
        if isinstance(exc, HttpUnavailable):
            instances = winipc.describe_instances()
            if instances["running"]:
                message = self._http_disabled_message(instances)
                return EverythingError(
                    message, kind="http_disabled", details={"instances": instances}
                )
            return EverythingError(
                f"{exc}\nEverything 未运行。请启动 {self.config.exe}，"
                "或设置 EV_AUTO_START=1 让本服务器自动拉起。",
                kind="not_running",
            )
        if isinstance(exc, HttpProtocolError):
            return EverythingError(
                f"{exc}\n请确认 Everything 版本 >= 1.4 且 HTTP Server 已启用。",
                kind="protocol",
            )
        return EverythingError(str(exc), kind="error")

    def _execute(self, **kwargs) -> dict:
        """执行一次 HTTP 查询；连接失败时尝试自动拉起并重试一次。"""
        try:
            return self.http.query(**kwargs)
        except HttpUnavailable as exc:
            logger.warning("HTTP 不可用，尝试恢复通道：%s", exc)
            self._ensure_channel()
            try:
                return self.http.query(**kwargs)
            except EverythingHttpError as retry_exc:
                raise self._convert_http_error(retry_exc) from retry_exc
        except HttpTimeout as exc:
            # 超时先原样重试一次（索引构建/磁盘繁忙时可能恢复）
            time.sleep(0.05)
            try:
                return self.http.query(**kwargs)
            except EverythingHttpError as retry_exc:
                raise self._convert_http_error(retry_exc) from retry_exc
        except EverythingHttpError as exc:
            raise self._convert_http_error(exc) from exc

    # ------------------------------------------------------------------
    # 查询参数准备（search / count / export / search_pages 共用）
    # ------------------------------------------------------------------
    def _prepare_search(
        self,
        query: str,
        *,
        regex: bool = False,
        case_sensitive: bool = False,
        whole_word: bool = False,
        match_path: bool = False,
        max_results: int = 100,
        offset: int = 0,
        sort: str = "",
        ascending: bool | None = None,
        path_scope: str = "",
        folder_only: bool = False,
        file_only: bool = False,
        fields=None,
        count_only: bool = False,
        legacy_columns=None,
        legacy_path_filter: str = "",
    ) -> dict:
        """校验/归一化参数、组合 effective_query，供各查询方法复用。"""
        if not isinstance(query, str):
            raise EverythingError("query 必须是字符串。", kind="bad_argument")
        warnings: list[str] = []

        if legacy_path_filter and not path_scope:
            path_scope = legacy_path_filter
            warnings.append(
                "path_filter 是 v1 兼容参数（已废弃），等价于 path_scope；建议改用 path_scope。"
            )
        if legacy_columns:
            warnings.append("columns 是 v1 兼容参数（已废弃），建议改用 fields。")
            if fields is None:
                merged = list(DEFAULT_FIELDS)
                for column in _split_list(legacy_columns):
                    if column not in merged:
                        merged.append(column)
                fields = merged

        field_list = _normalize_fields(fields, warnings)
        sort_key = _normalize_sort(sort, warnings)
        ascending_value: bool | None
        if ascending is None or isinstance(ascending, bool):
            ascending_value = ascending
        else:
            warnings.append(f"ascending={ascending!r} 不是布尔值，已忽略（使用默认方向）。")
            ascending_value = None
        folder, file_ = _resolve_ambiguous_bools(folder_only, file_only, warnings)

        scope_text = str(path_scope).strip() if path_scope else ""
        prefix = _normalize_prefix(scope_text)
        scope_query = _scope_for_query(scope_text) if scope_text else ""
        want_type = "folder" if folder else ("file" if file_ else None)

        # regex 模式下整串 query 都会被当正则，不能拼接 path:"..."/folder:/file:
        regex_client_side = bool(regex and (scope_query or want_type))
        if regex_client_side:
            warnings.append(
                "regex=True 时 Everything 会把整串查询当正则，无法附加 path_scope/"
                "folder_only/file_only 过滤；已改为对取回的候选做客户端过滤（结果为近似值，"
                "可能不完整）。"
            )

        parts: list[str] = []
        stripped_query = query.strip()
        if stripped_query:
            parts.append(stripped_query)
        if not regex:
            if scope_query:
                parts.append(f'path:"{scope_query}"')
            if folder:
                parts.append("folder:")
            elif file_:
                parts.append("file:")
        effective_query = " ".join(parts)

        # 客户端过滤需要 path 列；类型过滤依赖 type（始终返回）
        client_prefix_filter = bool(prefix)
        client_type_filter = bool(regex_client_side and want_type)
        fetch_fields = set(field_list)
        if client_prefix_filter or client_type_filter:
            fetch_fields.add("path")
        columns = [key for key in ("path", "size", "date_modified") if key in fetch_fields]

        cap = self.config.max_results_cap
        limit = _clamp_int(max_results, 1, cap, min(100, cap))
        offset_value = max(0, _clamp_int(offset, 0, 10**9, 0))
        approximate = regex_client_side
        if count_only:
            approximate = regex_client_side
        fetch = limit
        if approximate:
            # 客户端过滤时多取候选，尽量满足 limit
            fetch = min(cap, max(limit * 5, 500))

        return {
            "effective_query": effective_query,
            "regex": bool(regex),
            "case_sensitive": bool(case_sensitive),
            "whole_word": bool(whole_word),
            "match_path": bool(match_path),
            "sort": sort_key,
            "ascending": ascending_value,
            "path_scope": scope_text,
            "prefix": prefix,
            "folder_only": folder,
            "file_only": file_,
            "want_type": want_type,
            "client_prefix_filter": client_prefix_filter,
            "client_type_filter": client_type_filter,
            "approximate": approximate,
            "fields": field_list,
            "columns": columns,
            "limit": limit,
            "offset": offset_value,
            "fetch": fetch,
            "count_only": bool(count_only),
            "warnings": warnings,
        }

    # ------------------------------------------------------------------
    # search / count
    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        *,
        regex: bool = False,
        case_sensitive: bool = False,
        whole_word: bool = False,
        match_path: bool = False,
        max_results: int = 100,
        offset: int = 0,
        sort: str = "",
        ascending: bool | None = None,
        path_scope: str = "",
        folder_only: bool = False,
        file_only: bool = False,
        fields=None,
        count_only: bool = False,
        # v1 兼容参数（已废弃，映射到 path_scope / fields）
        columns=None,
        path_filter: str = "",
    ) -> dict:
        """搜索文件/目录。

        返回字段见 SPEC §2：``query``、``effective_query``、``regex``、
        ``case_sensitive``、``whole_word``、``match_path``、``path_scope``、
        ``sort``、``ascending``、``offset``、``next_offset``、``max_results``、
        ``total``、``returned``、``truncated``、``elapsed_ms``、``channel``、
        ``results[]``、``warnings[]``。
        """
        started = time.perf_counter()
        prepared = self._prepare_search(
            query,
            regex=regex,
            case_sensitive=case_sensitive,
            whole_word=whole_word,
            match_path=match_path,
            max_results=max_results,
            offset=offset,
            sort=sort,
            ascending=ascending,
            path_scope=path_scope,
            folder_only=folder_only,
            file_only=file_only,
            fields=fields,
            count_only=count_only,
            legacy_columns=columns,
            legacy_path_filter=path_filter,
        )
        warnings: list[str] = prepared["warnings"]
        results: list[dict] = []
        filtered_total: int | None = None
        fetched = 0
        if count_only:
            if prepared["approximate"]:
                # regex + path_scope/folder_only：与 count() 一致，用客户端过滤近似计数
                fetch = self.config.max_results_cap
                data = self._execute(
                    search=prepared["effective_query"],
                    count=fetch,
                    offset=0,
                    regex=prepared["regex"],
                    case=prepared["case_sensitive"],
                    whole_word=prepared["whole_word"],
                    match_path=prepared["match_path"],
                    sort=prepared["sort"],
                    ascending=prepared["ascending"],
                    columns=["path"],
                )
                matched = [
                    item
                    for item in data["results"]
                    if _match_client_filters(
                        item, prefix=prepared["prefix"], want_type=prepared["want_type"]
                    )
                ]
                total = len(matched)
                filtered_total = len(matched)
                warnings.append(
                    f"count_only 在 regex+path_scope/folder_only 模式下为近似值"
                    f"（最多检查 {fetch} 条候选）。"
                )
            else:
                data = self._execute(
                    search=prepared["effective_query"],
                    count=0,
                    offset=0,
                    regex=prepared["regex"],
                    case=prepared["case_sensitive"],
                    whole_word=prepared["whole_word"],
                    match_path=prepared["match_path"],
                    sort=prepared["sort"],
                    ascending=prepared["ascending"],
                    columns=[],
                )
                total = int(data["total"])
            next_offset = prepared["offset"]
        else:
            data = self._execute(
                search=prepared["effective_query"],
                count=prepared["fetch"],
                offset=prepared["offset"],
                regex=prepared["regex"],
                case=prepared["case_sensitive"],
                whole_word=prepared["whole_word"],
                match_path=prepared["match_path"],
                sort=prepared["sort"],
                ascending=prepared["ascending"],
                columns=prepared["columns"],
            )
            raw_results = data["results"]
            total = int(data["total"])
            fetched = len(raw_results)
            matched = [
                item
                for item in raw_results
                if _match_client_filters(
                    item, prefix=prepared["prefix"], want_type=prepared["want_type"]
                )
            ]
            if prepared["approximate"]:
                filtered_total = len(matched)
            dropped = fetched - len(matched)
            if dropped and not prepared["approximate"]:
                warnings.append(
                    f"有 {dropped} 条结果未通过 path_scope 前缀过滤已剔除"
                    '（Everything 的 path:"..." 是“包含”匹配，客户端按前缀双保险）。'
                )
            results = [_project_fields(item, prepared["fields"]) for item in matched]
            if prepared["approximate"]:
                results = results[: prepared["limit"]]
            next_offset = prepared["offset"] + fetched
        truncated = (not count_only) and (
            next_offset < total
            or (filtered_total is not None and filtered_total > len(results))
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        payload: dict[str, Any] = {
            "query": query,
            "effective_query": prepared["effective_query"],
            "regex": prepared["regex"],
            "case_sensitive": prepared["case_sensitive"],
            "whole_word": prepared["whole_word"],
            "match_path": prepared["match_path"],
            "path_scope": prepared["path_scope"] or None,
            "folder_only": prepared["folder_only"],
            "file_only": prepared["file_only"],
            "fields": prepared["fields"],
            "sort": prepared["sort"],
            "ascending": prepared["ascending"],
            "offset": prepared["offset"],
            "next_offset": next_offset,
            "max_results": prepared["limit"],
            "total": total,
            "returned": len(results),
            "truncated": truncated,
            "elapsed_ms": round(elapsed_ms, 3),
            "channel": "http",
            "results": results,
            "warnings": warnings,
        }
        if filtered_total is not None:
            payload["filtered_total"] = filtered_total
            payload["approx"] = True
        return payload

    def count(
        self,
        query: str,
        *,
        regex: bool = False,
        case_sensitive: bool = False,
        whole_word: bool = False,
        match_path: bool = False,
        path_scope: str = "",
        folder_only: bool = False,
        file_only: bool = False,
        sort: str = "",
        ascending: bool | None = None,
        path_filter: str = "",
    ) -> dict:
        """只返回命中数量（``count=0``，不请求结果列，最省 token）。"""
        started = time.perf_counter()
        prepared = self._prepare_search(
            query,
            regex=regex,
            case_sensitive=case_sensitive,
            whole_word=whole_word,
            match_path=match_path,
            max_results=1,
            offset=0,
            sort=sort,
            ascending=ascending,
            path_scope=path_scope,
            folder_only=folder_only,
            file_only=file_only,
            fields=["name"],
            count_only=True,
            legacy_path_filter=path_filter,
        )
        warnings: list[str] = prepared["warnings"]
        if prepared["approximate"]:
            fetch = self.config.max_results_cap
            data = self._execute(
                search=prepared["effective_query"],
                count=fetch,
                offset=0,
                regex=prepared["regex"],
                case=prepared["case_sensitive"],
                whole_word=prepared["whole_word"],
                match_path=prepared["match_path"],
                sort=prepared["sort"],
                ascending=prepared["ascending"],
                columns=["path"],
            )
            matched = [
                item
                for item in data["results"]
                if _match_client_filters(
                    item, prefix=prepared["prefix"], want_type=prepared["want_type"]
                )
            ]
            payload: dict[str, Any] = {
                "query": query,
                "effective_query": prepared["effective_query"],
                "total": len(matched),
                "total_unfiltered": int(data["total"]),
                "approx": True,
                "note": (
                    f"regex 模式下 path_scope/folder_only/file_only 是客户端过滤，"
                    f"最多检查 {fetch} 条候选；精确总数请去掉这些过滤条件后查询。"
                ),
                "warnings": warnings,
            }
        else:
            data = self._execute(
                search=prepared["effective_query"],
                count=0,
                offset=0,
                regex=prepared["regex"],
                case=prepared["case_sensitive"],
                whole_word=prepared["whole_word"],
                match_path=prepared["match_path"],
                sort=prepared["sort"],
                ascending=prepared["ascending"],
                columns=[],
            )
            payload = {
                "query": query,
                "effective_query": prepared["effective_query"],
                "total": int(data["total"]),
                "warnings": warnings,
            }
        payload["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        payload["channel"] = "http"
        return payload

    # ------------------------------------------------------------------
    # 分页生成器（task-4 的 summary/duplicates 依赖）
    # ------------------------------------------------------------------
    def search_pages(
        self,
        query: str,
        *,
        page_size: int = DEFAULT_PAGE_SIZE,
        max_items: int | None = None,
        regex: bool = False,
        case_sensitive: bool = False,
        whole_word: bool = False,
        match_path: bool = False,
        sort: str = "",
        ascending: bool | None = None,
        path_scope: str = "",
        folder_only: bool = False,
        file_only: bool = False,
        fields=None,
        path_filter: str = "",
    ) -> Iterator[list[dict]]:
        """按页迭代查询结果（每页 ≤``page_size`` 条，总量 ≤``max_items``）。

        供 aggregation/duplicates 扫描使用：调用方直接 ``for page in ...``，
        不一次性把整盘结果读进内存。默认 ``max_items=EV_SCAN_CAP``（50000）。
        """
        cap = self.config.max_results_cap
        page_limit = _clamp_int(page_size, 1, cap, min(DEFAULT_PAGE_SIZE, cap))
        total_cap = (
            max_items
            if isinstance(max_items, int) and not isinstance(max_items, bool) and max_items > 0
            else self.config.scan_cap
        )
        prepared = self._prepare_search(
            query,
            regex=regex,
            case_sensitive=case_sensitive,
            whole_word=whole_word,
            match_path=match_path,
            max_results=page_limit,
            offset=0,
            sort=sort,
            ascending=ascending,
            path_scope=path_scope,
            folder_only=folder_only,
            file_only=file_only,
            fields=fields,
            legacy_path_filter=path_filter,
        )
        offset = 0
        yielded = 0
        while yielded < total_cap:
            want = min(prepared["limit"], total_cap - yielded)
            data = self._execute(
                search=prepared["effective_query"],
                count=want,
                offset=offset,
                regex=prepared["regex"],
                case=prepared["case_sensitive"],
                whole_word=prepared["whole_word"],
                match_path=prepared["match_path"],
                sort=prepared["sort"],
                ascending=prepared["ascending"],
                columns=prepared["columns"],
            )
            raw_results = data["results"]
            total = int(data["total"])
            if not raw_results:
                break
            matched = [
                item
                for item in raw_results
                if _match_client_filters(
                    item, prefix=prepared["prefix"], want_type=prepared["want_type"]
                )
            ]
            offset += len(raw_results)
            if matched:
                page = [_project_fields(item, prepared["fields"]) for item in matched]
                yield page
                yielded += len(page)
            if offset >= total:
                break

    # ------------------------------------------------------------------
    # status 辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _read_ini_map(path: Path) -> dict[str, str]:
        mapping: dict[str, str] = {}
        try:
            text = path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            return mapping
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith((";", "#")) or "=" not in line:
                continue
            key, _, value = line.partition("=")
            mapping[key.strip().lower()] = value.strip()
        return mapping

    def _ini_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        if self.config.config_ini:
            candidates.append(Path(self.config.config_ini))
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "Everything" / "Everything.ini")
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            candidates.append(Path(localappdata) / "Everything" / "Everything.ini")
        candidates.append(Path(self.config.exe).parent / "Everything.ini")
        deduped: list[Path] = []
        for candidate in candidates:
            if candidate not in deduped:
                deduped.append(candidate)
        return deduped

    def _ini_info(self) -> dict:
        candidates = self._ini_candidates()
        merged: dict[str, str] = {}
        sources: list[str] = []
        for candidate in candidates:
            if candidate.is_file():
                sources.append(str(candidate))
                merged.update(self._read_ini_map(candidate))
        switch_keys = (
            "index_size",
            "index_date_modified",
            "index_date_created",
            "index_date_accessed",
            "index_attributes",
            "index_folder_size",
        )
        http_keys = (
            "http_server_enabled",
            "http_server_port",
            "http_server_bindings",
            "http_server_allow_file_download",
        )
        return {
            "path": sources[-1] if sources else (str(candidates[0]) if candidates else None),
            "exists": bool(sources),
            "sources": sources,
            "switches": {key: merged.get(key) for key in switch_keys},
            "http": {key: merged.get(key) for key in http_keys},
        }

    def _index_db_info(self) -> dict:
        candidates: list[Path] = []
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            candidates.append(Path(localappdata) / "Everything" / "Everything.db")
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "Everything" / "Everything.db")
        candidates.append(Path(self.config.exe).parent / "Everything.db")
        details: list[dict] = []
        chosen: dict | None = None
        for candidate in candidates:
            if candidate in [Path(item["path"]) for item in details]:
                continue
            exists = candidate.is_file()
            size: int | None = None
            if exists:
                try:
                    size = candidate.stat().st_size
                except OSError:
                    size = None
            item = {
                "path": str(candidate),
                "exists": exists,
                "size_bytes": size,
                "size_mb": round(size / (1024 * 1024), 2) if size is not None else None,
            }
            details.append(item)
            if exists and chosen is None:
                chosen = item
        if chosen is None:
            chosen = details[0] if details else {"path": None, "exists": False}
        return {
            "path": chosen.get("path"),
            "exists": bool(chosen.get("exists")),
            "size_bytes": chosen.get("size_bytes"),
            "size_mb": chosen.get("size_mb"),
            "candidates": details,
        }

    @staticmethod
    def _volumes_info() -> list[dict]:
        if os.name != "nt":
            return []
        type_names = {
            0: "unknown",
            1: "no_root",
            2: "removable",
            3: "fixed",
            4: "network",
            5: "cdrom",
            6: "ramdisk",
        }
        try:
            kernel32 = ctypes.windll.kernel32
            mask = int(kernel32.GetLogicalDrives())
        except Exception:  # noqa: BLE001 - 诊断信息失败不应影响 status
            return []
        volumes: list[dict] = []
        for index in range(26):
            if not (mask >> index) & 1:
                continue
            letter = chr(ord("A") + index)
            root = f"{letter}:\\"
            try:
                code = int(kernel32.GetDriveTypeW(root))
            except Exception:  # noqa: BLE001
                code = 0
            volumes.append(
                {"drive": f"{letter}:", "root": root, "type": type_names.get(code, "unknown")}
            )
        return volumes

    def _rg_info(self) -> dict:
        path, source = find_rg(self.config.rg_exe)
        info: dict[str, Any] = {
            "available": bool(path),
            "path": path,
            "source": source,
        }
        if path:
            try:
                completed = subprocess.run(
                    [path, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                )
                first_line = (completed.stdout or "").splitlines()
                info["version"] = first_line[0].strip() if first_line else None
            except (OSError, subprocess.SubprocessError):
                info["version"] = None
        return info

    def _presets_info(self) -> dict:
        try:
            data = presets_module.describe(self.config)
        except Exception as exc:  # noqa: BLE001 - 预设文件损坏不应影响 status
            return {"count": None, "error": str(exc), "warnings": []}
        return {
            "count": data.get("count"),
            "builtin_count": data.get("builtin_count"),
            "custom_count": data.get("custom_count"),
            "custom_file": data.get("custom_file"),
            "warnings": data.get("warnings") or [],
        }

    def status(self) -> dict:
        """返回 Everything 运行状态、HTTP 可达性、版本、索引、DB、卷、ini 开关、
        rg、预设、HTTP 能力矩阵摘要与可操作提示。"""
        started = time.perf_counter()
        reachable = self.http.ping()
        instances = winipc.describe_instances()
        # HTTP 可达本身就能证明 Everything 在运行（进程可能属于其他会话/用户，
        # 仅凭进程/窗口枚举不一定能看到，因此两者取并集）。
        running = bool(instances["running"] or reachable)
        detected_by = []
        if instances.get("processes"):
            detected_by.append("process")
        if instances.get("windows"):
            detected_by.append("window")
        if reachable:
            detected_by.append("http")
        exe_path = Path(self.config.exe)
        version = winipc.get_file_version(exe_path) if exe_path.is_file() else None

        index: dict = {"total_items": None, "ready": False}
        http_error: str | None = None
        if reachable:
            try:
                total = self.http.total("")
                index["total_items"] = total
                index["ready"] = total > 0
            except EverythingHttpError as exc:
                http_error = str(exc)
        else:
            index["hint"] = (
                "HTTP Server 不可达；若 Everything 已运行，请检查 ini 中 "
                "http_server_enabled=1 / http_server_port / http_server_bindings（只写 IP）。"
            )

        ini_info = self._ini_info()
        db_info = self._index_db_info()
        volumes = self._volumes_info()
        rg_info = self._rg_info()
        presets_info = self._presets_info()

        hints: list[str] = []
        if not running:
            hints.append(
                f"Everything 未运行：请启动 {self.config.exe}（或 EV_AUTO_START=1 让首次查询自动拉起）。"
            )
        elif not reachable:
            hints.append(self._http_disabled_message(instances))
        elif index["ready"] is False:
            hints.append(
                "索引条目数为 0：Everything 可能仍在构建索引（首次启动或重建），稍后重试。"
            )

        switches = ini_info.get("switches") or {}
        disabled = [
            key
            for key in ("index_date_created", "index_date_accessed", "index_attributes")
            if str(switches.get(key) or "0").strip() not in {"1", "true", "yes"}
        ]
        if disabled:
            hints.append(
                f"以下索引开关关闭：{', '.join(disabled)}；因此 dc:/da:/attrib: 查询极慢。"
                "如需启用：完全退出 Everything → 修改 Everything.ini 对应项为 1 → "
                "重启并重建索引（详见 everything_help 的慢查询说明）。"
            )
        ini_http = ini_info.get("http") or {}
        if str(ini_http.get("http_server_allow_file_download") or "0").strip() not in {
            "1",
            "true",
            "yes",
        }:
            hints.append(
                "Everything.ini 未开启 http_server_allow_file_download=1："
                "everything_download 会失败；请在 Everything 选项 → HTTP Server 中允许文件下载。"
            )
        if not rg_info.get("available"):
            hints.append(
                "未找到 ripgrep（rg.exe）：everything_grep 不可用。请安装 ripgrep，"
                "或设置 EV_RG_EXE 指向 rg.exe（DSH 自带路径通常位于 "
                "%APPDATA%\\npm\\node_modules\\@deepseek-ai\\dsh\\node_modules\\"
                "@vscode\\ripgrep-win32-x64\\bin\\rg.exe）。"
            )
        for warning in presets_info.get("warnings") or []:
            if len(hints) < 10:
                hints.append(f"预设：{warning}")

        return {
            "ok": reachable,
            "channel": "http" if reachable else None,
            "http": {
                "url": self.config.http_url,
                "host": self.config.host,
                "port": self.config.port,
                "reachable": reachable,
                "error": http_error,
            },
            "everything": {
                "running": running,
                "detected_by": detected_by,
                "processes": instances["processes"],
                "windows": instances["windows"],
                "instances": instances["instances"],
                "exe": str(exe_path),
                "exe_exists": exe_path.is_file(),
                "version": version,
            },
            "index": index,
            "index_db": db_info,
            "index_switches": switches,
            "ini": ini_info,
            "volumes": volumes,
            "rg": rg_info,
            "presets": presets_info,
            "capabilities": help_module.capability_summary(),
            "auto_start": {
                "enabled": self.config.auto_start,
                "started_by_server": self.started_by_server,
                "started_pid": self._started_pid,
                "started_at": self._started_at,
                "last_error": self._last_error,
            },
            "config": self.config.describe(),
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "hints": hints,
        }

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def _resolve_export_path(self, out_csv: str | Path, format: str) -> Path:
        if not out_csv or not str(out_csv).strip():
            raise EverythingError("out_csv 不能为空。", kind="bad_argument")
        path = Path(str(out_csv).strip())
        if not path.is_absolute():
            path = self.config.export_root / path
        suffix = EXPORT_SUFFIX[format]
        if path.suffix.lower() != suffix:
            path = path.with_suffix(suffix)
        try:
            resolved = path.resolve()
        except OSError as exc:
            raise EverythingError(f"无法解析导出路径：{exc}", kind="bad_argument") from exc
        root = self.config.export_root.resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            raise EverythingError(
                f"拒绝导出到工作区外：{resolved}\n允许的根目录：{root}"
                "（可用 EV_EXPORT_ROOT 覆盖）。",
                kind="export_outside_workspace",
            ) from None
        return resolved

    @staticmethod
    def _write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({key: row.get(key, "") for key in fieldnames})

    @staticmethod
    def _write_jsonl(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
        with path.open("w", encoding="utf-8", newline="") as handle:
            for row in rows:
                payload = {
                    key: row.get(key)
                    for key in fieldnames
                    if key in row
                }
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    @staticmethod
    def _write_efu(path: Path, rows: list[dict]) -> None:
        """写 Everything 文件列表（.efu）：Filename,Size,Date Modified,..."""
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["Filename", "Size", "Date Modified", "Date Created", "Attributes"]
            )
            for row in rows:
                modified = iso_to_filetime(row.get("date_modified") or "")
                size = row.get("size")
                writer.writerow(
                    [
                        row.get("path") or row.get("name") or "",
                        size if isinstance(size, int) else "",
                        modified if modified is not None else "",
                        "",
                        "",
                    ]
                )

    def export(
        self,
        query: str,
        out_csv: str | None = None,
        *,
        out_path: str | None = None,
        format: str = "csv",
        max_results: int = 5000,
        offset: int = 0,
        regex: bool = False,
        case_sensitive: bool = False,
        whole_word: bool = False,
        match_path: bool = False,
        sort: str = "",
        ascending: bool | None = None,
        columns=None,
        path_scope: str = "",
        folder_only: bool = False,
        file_only: bool = False,
        fields=None,
        # v1 兼容参数
        path_filter: str = "",
    ) -> dict:
        """把命中结果分页导出为 csv / jsonl / efu。

        - 仅允许写 ``EV_EXPORT_ROOT``（默认工作区）内的文件；
        - ``format``：``csv``（UTF-8 BOM，Excel 可直接打开）、``jsonl``（每行一个
          对象）、``efu``（Everything 文件列表，日期为 FILETIME）；
        - ``offset`` 支持从任意位置开始导出。
        """
        started = time.perf_counter()
        fmt = str(format or "csv").strip().lower()
        if fmt not in EXPORT_FORMATS:
            raise EverythingError(
                f"不支持的导出格式：{format!r}。可用格式：{list(EXPORT_FORMATS)}。",
                kind="bad_argument",
            )
        target = out_csv if out_csv is not None else out_path
        if target is None:
            raise EverythingError(
                "缺少导出目标路径：请传 out_csv（或 out_path）。", kind="bad_argument"
            )
        limit = _clamp_int(max_results, 1, EXPORT_HARD_CAP, 5000)
        efu_mode = fmt == "efu"
        prepared = self._prepare_search(
            query,
            regex=regex,
            case_sensitive=case_sensitive,
            whole_word=whole_word,
            match_path=match_path,
            max_results=limit,
            offset=offset,
            sort=sort,
            ascending=ascending,
            path_scope=path_scope,
            folder_only=folder_only,
            file_only=file_only,
            fields=(["name", "path", "size", "date_modified"] if efu_mode else fields),
            legacy_columns=(None if efu_mode else columns),
            legacy_path_filter=path_filter,
        )
        warnings: list[str] = prepared["warnings"]
        if efu_mode:
            if columns is not None:
                warnings.append(
                    "efu 格式固定包含 Filename/Size/Date Modified 三列，"
                    "columns 参数已忽略。"
                )
            output_fields = ["name", "path", "size", "date_modified"]
        elif columns is not None:
            # export 的 columns 是“输出列子集”（保持调用方顺序）
            output_fields = _normalize_fields(columns, warnings) or ["name"]
        elif fields is not None:
            output_fields = prepared["fields"] or ["name"]
        else:
            output_fields = list(DEFAULT_FIELDS)
        out_file = self._resolve_export_path(target, fmt)

        rows: list[dict] = []
        total = 0
        scan_offset = prepared["offset"]
        page_size = 500
        while len(rows) < limit:
            want = min(page_size, limit - len(rows))
            data = self._execute(
                search=prepared["effective_query"],
                count=want,
                offset=scan_offset,
                regex=prepared["regex"],
                case=prepared["case_sensitive"],
                whole_word=prepared["whole_word"],
                match_path=prepared["match_path"],
                sort=prepared["sort"],
                ascending=prepared["ascending"],
                columns=prepared["columns"],
            )
            total = int(data["total"])
            raw_results = data["results"]
            if not raw_results:
                break
            matched = [
                item
                for item in raw_results
                if _match_client_filters(
                    item, prefix=prepared["prefix"], want_type=prepared["want_type"]
                )
            ]
            rows.extend(_project_fields(item, output_fields) for item in matched)
            scan_offset += len(raw_results)
            if scan_offset >= total:
                break
        rows = rows[:limit]
        truncated = (prepared["offset"] + len(rows)) < total

        out_file.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [key for key in output_fields if key in SUPPORTED_FIELDS] or ["name"]
        if fmt == "csv":
            self._write_csv(out_file, rows, fieldnames)
        elif fmt == "jsonl":
            self._write_jsonl(out_file, rows, fieldnames)
        else:
            self._write_efu(out_file, rows)

        try:
            byte_count = out_file.stat().st_size
        except OSError:
            byte_count = 0
        return {
            "ok": True,
            "format": fmt,
            "path": str(out_file),
            "rows": len(rows),
            "bytes": byte_count,
            "total": total,
            "offset": prepared["offset"],
            "truncated": truncated,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "channel": "http",
            "warnings": warnings,
        }

    # ------------------------------------------------------------------
    # 下载（Everything HTTP Server 的文件服务能力）
    # ------------------------------------------------------------------
    def _resolve_download_path(self, dest: str, source: Path) -> Path:
        root = self.config.download_root.resolve()
        text = str(dest or "").strip()
        if not text:
            candidate = root / source.name
        else:
            path = Path(text)
            candidate = path if path.is_absolute() else root / path
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise EverythingError(f"无法解析下载路径：{exc}", kind="bad_argument") from exc
        try:
            resolved.relative_to(root)
        except ValueError:
            raise EverythingError(
                f"拒绝下载到下载根目录之外：{resolved}\n允许的根目录：{root}"
                "（可用 EV_DOWNLOAD_ROOT 覆盖）。",
                kind="download_outside_root",
            ) from None
        return resolved

    @staticmethod
    def _unique_path(path: Path) -> Path:
        if not path.exists():
            return path
        stem, suffix = path.stem, path.suffix
        for index in range(1, 1000):
            candidate = path.with_name(f"{stem} ({index}){suffix}")
            if not candidate.exists():
                return candidate
        raise EverythingError(
            f"下载目标文件名冲突过多，无法生成唯一文件名：{path}", kind="bad_argument"
        )

    def download(self, path: str, dest: str = "", *, max_bytes: int = 0) -> dict:
        """经 Everything HTTP Server 下载 ``path`` 到 ``EV_DOWNLOAD_ROOT`` 内。

        只允许文件（目录会被拒绝）；目标必须位于下载根目录内；超过
        ``max_bytes``（默认 2 GiB）会中止并清理半成品。
        """
        started = time.perf_counter()
        if not isinstance(path, str) or not path.strip():
            raise EverythingError("path 不能为空。", kind="bad_argument")
        source = Path(path.strip())
        if not source.is_absolute():
            raise EverythingError(
                f"path 必须是搜索得到的绝对路径：{path!r}", kind="bad_argument"
            )
        if source.is_dir():
            raise EverythingError(
                f"path 是目录，不是文件；everything_download 只下载文件：{source}",
                kind="bad_argument",
            )
        if not source.is_file():
            raise EverythingError(
                f"文件不存在（可能已移动或删除，请重新搜索）：{source}",
                kind="not_found",
            )
        limit = int(max_bytes) if isinstance(max_bytes, int) and max_bytes > 0 else DOWNLOAD_MAX_BYTES
        destination = self._resolve_download_path(dest, source)
        if destination.is_dir():
            raise EverythingError(
                f"下载目标必须是文件路径，不是已存在的目录：{destination}",
                kind="bad_argument",
            )
        destination = self._unique_path(destination)

        try:
            result = self.http.download_file(source, destination, max_bytes=limit)
        except HttpNotFound as exc:
            raise EverythingError(
                f"{exc}\n文件可能已被移动/删除，或 Everything 索引尚未刷新；"
                "请重新 everything_search 后再下载。",
                kind="not_found",
            ) from exc
        except HttpTooLarge as exc:
            raise EverythingError(
                f"{exc}\n可调大 max_bytes（当前上限 {limit} 字节）或改用其他方式获取。",
                kind="too_large",
            ) from exc
        except HttpTimeout as exc:
            raise EverythingError(
                f"{exc}\n文件较大或磁盘繁忙；可重试，或调大 EV_HTTP_TIMEOUT。",
                kind="timeout",
            ) from exc
        except EverythingHttpError as exc:
            raise self._convert_http_error(exc) from exc

        return {
            "ok": True,
            "source": str(source),
            "saved_path": str(destination),
            "bytes": result["bytes"],
            "size": result["bytes"],
            "max_bytes": limit,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "channel": "http",
            "warnings": [],
        }
