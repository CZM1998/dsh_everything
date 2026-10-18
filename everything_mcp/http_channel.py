"""Everything 内置 HTTP Server 查询通道（纯标准库 urllib）。

Everything 1.4/1.5 自带 HTTP Server，启用后可通过 HTTP GET 查询索引：

    GET /?search=<查询>&json=1&count=<条数>&offset=<起点>
        &path_column=1&size_column=1&date_modified_column=1
        &regex=1&case=1&wholeword=1&path=1&sort=<列>&ascending=0|1

响应（UTF-8 JSON）：

    {"totalResults": 123, "results": [
        {"type": "file", "name": "Everything.exe",
         "path": "C:\\\\Program Files\\\\Everything", "size": "2265096",
         "date_modified": "133295352000000000"}
    ]}

已核实（Everything 1.4.1.1024，2026-10-18）：
- 只有 ``path_column`` / ``size_column`` / ``date_modified_column`` 三个列参数生效；
  ``date_created_column`` / ``date_accessed_column`` / ``attributes_column`` 等
  1.4 静默忽略，**不要**下发。
- ``count`` 无硬上限（1000/2000/5000/10000 均按请求返回）；``count=0`` 只返回
  ``totalResults``（``results`` 为空数组）；负数/非法值返回 0 条。
- ``offset`` / ``sort`` / ``ascending`` 生效；``sort`` 仅支持
  ``name`` / ``path`` / ``size`` / ``date_modified``，其余静默回退 name 升序；
  ``ascending`` 缺省为升序。
- ``path=1`` 是 **match-path 搜索开关**（必须且只能是布尔真值 1；
  ``path=true`` / ``matchpath=1`` 实测均**无效**），不是路径过滤；
  ``path=<路径>`` / ``filter`` 参数无效，路径范围请用搜索语法 ``path:"..."``。
- ``size`` 以字符串返回（字节）；``date_modified`` 是 Windows FILETIME 字符串
  （1601-01-01 起 100ns），本模块统一转成整数与 ISO 8601 UTC。
- 文件下载 URL 形如 ``http://127.0.0.1:8090/C%3A/Program%20Files/.../x.exe``
  （``/`` + URL 编码的盘符与路径；``http_server_allow_file_download=1`` 已开启）。
  下载必须用 GET（HEAD 返回 400），不存在的文件返回 404。

说明：
- 只依赖标准库，毫秒级返回；本机 loopback 实测单次查询 1~5ms。
- 若 Everything 未启用 HTTP Server，连接会被拒绝，抛 ``HttpUnavailable``；
  超时抛 ``HttpTimeout``；返回非 JSON 或 4xx/5xx 抛 ``HttpProtocolError``；
  下载 404 抛 ``HttpNotFound``；超过大小上限抛 ``HttpTooLarge``。
  上层 ``client`` 会把它们转换成可操作的中文错误提示。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Everything HTTP Server 接受的 sort 值（1.4 实测生效的四项）+ 别名归一化。
# 注意：date_created / date_accessed / extension / type / run_count 等 1.5 特性
# 在 1.4 HTTP 上不生效，client 层会把它们降级为 warnings 并回退 name。
SUPPORTED_SORT_KEYS = ("name", "path", "size", "date_modified")

SORT_ALIASES = {
    "name": "name",
    "文件名": "name",
    "path": "path",
    "路径": "path",
    "size": "size",
    "大小": "size",
    "date_modified": "date_modified",
    "modified": "date_modified",
    "修改时间": "date_modified",
    # 以下为 1.5 特性/不生效别名，保留映射便于 client 生成准确告警
    "date_created": "date_created",
    "created": "date_created",
    "创建时间": "date_created",
    "date_accessed": "date_accessed",
    "accessed": "date_accessed",
    "extension": "extension",
    "ext": "extension",
    "type": "type",
    "run_count": "run_count",
    "date_run": "date_run",
    "date_recently_changed": "date_recently_changed",
}

# 结果可选列 -> Everything HTTP 参数名（仅前三项在 1.4 生效）
SUPPORTED_COLUMN_KEYS = ("path", "size", "date_modified")

COLUMN_PARAMS = {
    "path": "path_column",
    "size": "size_column",
    "date_modified": "date_modified_column",
    # 1.4 HTTP 不支持（静默忽略），保留定义仅供诊断展示
    "date_created": "date_created_column",
    "date_accessed": "date_accessed_column",
    "attributes": "attributes_column",
}

# 下载分块大小（256 KiB）
DOWNLOAD_CHUNK_SIZE = 256 * 1024


class EverythingHttpError(RuntimeError):
    """HTTP 通道错误基类。"""


class HttpUnavailable(EverythingHttpError):
    """无法连接 Everything HTTP Server（未启动 / 未启用 / 端口不对）。"""


class HttpTimeout(EverythingHttpError):
    """HTTP 查询超时。"""


class HttpProtocolError(EverythingHttpError):
    """Everything 返回了非预期内容。"""


class HttpNotFound(EverythingHttpError):
    """请求的文件在 Everything HTTP Server 上不存在（HTTP 404）。"""


class HttpTooLarge(EverythingHttpError):
    """文件超过允许的大小上限。"""


def _decode_bytes(raw: bytes) -> str:
    """Everything 默认输出 UTF-8；为兼容旧版本保留 GBK 兜底。"""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return raw.decode("gbk")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")


def _join_path(base: str, name: str) -> str:
    """拼接目录与文件名；Everything 返回的 path 通常不带尾部反斜杠。"""
    if not base:
        return name
    if base.endswith("\\") or base.endswith("/"):
        return base + name
    return base + "\\" + name


def _normalize_type(value: Any) -> str:
    """把 type 归一化为 'file' / 'folder' / 'drive'。

    Everything 1.4 的 JSON 有时给字符串（"file"），有时给整数代码
    （0=文件，1=文件夹，2=驱动器）。无法识别时回退 'file'。
    """
    if isinstance(value, str):
        return value.lower()
    if isinstance(value, int):
        return {0: "file", 1: "folder", 2: "drive"}.get(value, "file")
    return "file"


_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def _digits_to_int(value: str) -> int | None:
    text = value.strip().replace(",", "")
    if text.isdigit():
        try:
            return int(text)
        except ValueError:
            return None
    return None


def _filetime_to_iso(value: str) -> str | None:
    """把 Everything 返回的 Windows FILETIME 字符串（100ns ticks）转成 ISO 8601 UTC。

    Everything HTTP Server 的 JSON 默认把 size/date 返回为字符串，
    date_modified 形如 "133295352000000000"（1601-01-01 起的 100ns 数）。
    """
    ticks = _digits_to_int(value)
    if ticks is None or ticks <= 0 or len(value.strip()) < 15:
        return None
    try:
        moment = _FILETIME_EPOCH + timedelta(microseconds=ticks / 10)
    except (OverflowError, ValueError):
        return None
    return moment.isoformat().replace("+00:00", "Z")


def iso_to_filetime(value: str) -> int | None:
    """ISO 8601（如 ``2023-05-26T00:40:00Z``）→ Windows FILETIME ticks。

    供 ``everything_export(format="efu")`` 写 Everything 文件列表使用。
    """
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    delta = moment - _FILETIME_EPOCH
    return int(delta.total_seconds() * 10_000_000)


def _normalize_item(item: dict, columns: set[str] | None = None) -> dict:
    """把一条 Everything 结果归一化为稳定的输出结构。

    ``columns`` 为本次实际下发的列参数集合；``None`` 表示三列全请求。
    ``name`` / ``type`` 始终存在；``path`` / ``size`` / ``date_modified``
    只在对应列被请求（或响应里带值）时出现。
    """
    name = str(item.get("name") or "")
    base = str(item.get("path") or "")
    result: dict[str, Any] = {
        "name": name,
        "type": _normalize_type(item.get("type")),
    }
    if columns is None or "path" in columns:
        result["path"] = _join_path(base, name)
    # size：Everything HTTP JSON 返回字符串，这里转成整数字节数
    if item.get("size") not in (None, ""):
        raw_size = item["size"]
        if isinstance(raw_size, int):
            result["size"] = raw_size
        else:
            parsed = _digits_to_int(str(raw_size))
            result["size"] = parsed if parsed is not None else raw_size
    # 日期：Everything 返回 FILETIME 字符串，这里转成 ISO 8601 UTC
    for key in ("date_modified", "date_created", "date_accessed"):
        raw_date = item.get(key)
        if raw_date in (None, ""):
            continue
        if isinstance(raw_date, str):
            iso = _filetime_to_iso(raw_date)
            result[key] = iso if iso else raw_date
        else:
            result[key] = raw_date
    if item.get("attributes") not in (None, ""):
        result["attributes"] = item["attributes"]
    if name:
        dot = name.rfind(".")
        if 0 < dot < len(name) - 1:
            result.setdefault("extension", name[dot + 1:].lower())
    return result


class EverythingHttpChannel:
    """Everything HTTP Server 客户端。"""

    name = "http"

    def __init__(self, host: str = "127.0.0.1", port: int = 8090, timeout: float = 2.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        # 显式禁用代理：Everything 在 loopback 上，不应走系统代理
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    # ------------------------------------------------------------------
    # 底层 HTTP
    # ------------------------------------------------------------------
    def _get(self, params: dict[str, str]) -> dict:
        """发起一次 GET 并解析 JSON。"""
        url = self.base_url + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "everything-mcp/0.2 (+stdio)",
                "Accept": "application/json",
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise HttpProtocolError(
                f"Everything HTTP Server 返回 HTTP {exc.code}（{url}）"
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise HttpTimeout(
                f"Everything HTTP 查询超时（>{self.timeout:.1f}s）：{url}"
            ) from exc
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise HttpUnavailable(
                f"无法连接 Everything HTTP Server（{self.base_url}）：{reason}"
            ) from exc
        if status != 200:
            raise HttpProtocolError(
                f"Everything HTTP Server 返回异常状态码 {status}（{url}）"
            )
        text = _decode_bytes(raw)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            snippet = text[:120].replace("\n", " ")
            raise HttpProtocolError(
                f"Everything HTTP Server 返回的不是 JSON（{snippet!r}），"
                "请确认 URL 带有 json=1。"
            ) from exc
        if not isinstance(data, dict) or "results" not in data:
            raise HttpProtocolError(
                "Everything HTTP 返回结构缺少 results 字段，"
                "请确认 Everything 版本 >= 1.4 且 URL 带有 json=1。"
            )
        return data

    # ------------------------------------------------------------------
    # 对外查询接口
    # ------------------------------------------------------------------
    def ping(self) -> bool:
        """探测 HTTP 是否可用（低成本查询，1 条结果）。"""
        try:
            self._get(
                {
                    "search": "",
                    "json": "1",
                    "count": "1",
                    "path_column": "1",
                }
            )
            return True
        except EverythingHttpError:
            return False

    def query(
        self,
        search: str = "",
        count: int = 100,
        offset: int = 0,
        regex: bool = False,
        case: bool = False,
        whole_word: bool = False,
        match_path: bool = False,
        sort: str = "",
        ascending: bool | None = None,
        columns: list[str] | None = None,
    ) -> dict:
        """执行一次查询，返回 ``{"total": int, "results": [...]}``。

        参数直接映射到 Everything HTTP Server：

        - ``count=0`` 只取 ``totalResults``（分页/计数用）；
        - ``columns=None`` 默认请求 path/size/date_modified 三列（v1 兼容）；
          ``columns=[]`` 表示不请求任何列（最省带宽）；只下发实际请求的列；
        - ``offset`` / ``sort`` / ``ascending`` 原样下发（sort 白名单由 client 把关）。
        """
        column_set: set[str] | None
        if columns is None:
            column_set = None
        else:
            column_set = {str(c).strip().lower() for c in columns if str(c).strip()}
        params: dict[str, str] = {
            "search": search,
            "json": "1",
            "count": str(max(0, int(count))),
            "offset": str(max(0, int(offset))),
        }
        if column_set is None:
            for key in SUPPORTED_COLUMN_KEYS:
                params[COLUMN_PARAMS[key]] = "1"
        else:
            for key in SUPPORTED_COLUMN_KEYS:
                if key in column_set:
                    params[COLUMN_PARAMS[key]] = "1"
        if regex:
            params["regex"] = "1"
        if case:
            params["case"] = "1"
        if whole_word:
            params["wholeword"] = "1"
        if match_path:
            # 注意：Everything 1.4 HTTP 只认 path=1（布尔真值）作为 match-path 开关；
            # matchpath=1 无效（实测 search=blender&matchpath=1 仍为 620），path=true 也无效。
            # 该参数与搜索语法 path:"..." 无关，两者互不影响。
            params["path"] = "1"
        if sort:
            params["sort"] = sort
        if ascending is not None:
            params["ascending"] = "1" if ascending else "0"

        data = self._get(params)
        results = data.get("results") or []
        if not isinstance(results, list):
            raise HttpProtocolError("Everything HTTP 返回的 results 不是数组。")
        return {
            "total": int(data.get("totalResults") or 0),
            "results": [
                _normalize_item(item, column_set)
                for item in results
                if isinstance(item, dict)
            ],
        }

    def total(
        self,
        search: str = "",
        *,
        regex: bool = False,
        case: bool = False,
        whole_word: bool = False,
        match_path: bool = False,
    ) -> int:
        """只取命中总数（``count=0``，不请求任何列）。"""
        data = self.query(
            search=search,
            count=0,
            offset=0,
            regex=regex,
            case=case,
            whole_word=whole_word,
            match_path=match_path,
            columns=[],
        )
        return int(data["total"])

    # 兼容别名
    raw_query = query

    # ------------------------------------------------------------------
    # 文件下载（Everything HTTP Server 的文件服务能力）
    # ------------------------------------------------------------------
    def file_url(self, path: str) -> str:
        """构造 Everything HTTP Server 的文件下载 URL。

        形式：``http://127.0.0.1:8090/C%3A/Program%20Files/x.exe``
        （盘符冒号与空格均 URL 编码，路径分隔符统一为 ``/``）。
        """
        text = str(path).strip().replace("\\", "/")
        text = text.lstrip("/")
        encoded = urllib.parse.quote(text, safe="/")
        return self.base_url + encoded

    def download_file(
        self,
        path: str,
        dest: str | Path,
        *,
        max_bytes: int,
        timeout: float | None = None,
    ) -> dict:
        """通过 HTTP Server 把 ``path`` 下载到本地 ``dest``。

        返回 ``{"bytes": int, "content_type": str | None}``。
        404 → ``HttpNotFound``；超过 ``max_bytes`` → ``HttpTooLarge``（并清理半成品）。
        """
        url = self.file_url(path)
        destination = Path(dest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        effective_timeout = timeout if timeout is not None else max(10.0, self.timeout * 10)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "everything-mcp/0.2 (+stdio)",
                "Accept": "application/octet-stream",
            },
        )
        written = 0
        content_type: str | None = None
        try:
            with self._opener.open(request, timeout=effective_timeout) as response:
                content_type = response.headers.get("Content-Type")
                length_header = response.headers.get("Content-Length")
                expected: int | None = None
                if length_header:
                    try:
                        expected = int(length_header)
                    except ValueError:
                        expected = None
                if expected is not None and expected > max_bytes:
                    raise HttpTooLarge(
                        f"文件大小 {expected} 字节超过下载上限 {max_bytes} 字节：{path}"
                    )
                with destination.open("wb") as handle:
                    while True:
                        chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > max_bytes:
                            raise HttpTooLarge(
                                f"文件超过下载上限 {max_bytes} 字节：{path}"
                            )
                        handle.write(chunk)
        except urllib.error.HTTPError as exc:
            destination.unlink(missing_ok=True)
            if exc.code == 404:
                raise HttpNotFound(
                    f"Everything HTTP Server 上找不到该文件（HTTP 404）：{path}"
                ) from exc
            raise HttpProtocolError(
                f"Everything HTTP Server 下载失败（HTTP {exc.code}）：{path}"
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            destination.unlink(missing_ok=True)
            raise HttpTimeout(
                f"下载超时（>{effective_timeout:.0f}s）：{path}"
            ) from exc
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            destination.unlink(missing_ok=True)
            reason = getattr(exc, "reason", exc)
            raise HttpUnavailable(
                f"无法连接 Everything HTTP Server（{self.base_url}）：{reason}"
            ) from exc
        except HttpTooLarge:
            destination.unlink(missing_ok=True)
            raise
        return {"bytes": written, "content_type": content_type}
