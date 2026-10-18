#!/usr/bin/env python
"""everything-mcp 自测脚本（MCP 完整握手，v1 26 项 + v2 扩展 100+ 项断言）。

覆盖：
1. 完成 MCP 握手：initialize → notifications/initialized → tools/list → tools/call
   与 resources/list → resources/read；
2. v1 必须通过项（原 26 项，不依赖索引是否构建完成）：
   - 4 个工具全部注册；
   - everything_status 报告 HTTP 可达、Everything 在运行、版本号；
   - everything_search / everything_count 返回结构合法（允许 0 命中）；
   - everything_export 能在工作区写 CSV，且拒绝导出到工作区外；
   - 故意配置错误端口时返回可操作错误；
   - `python -m everything_mcp` 入口可用、`python everything-mcp/server.py` 可启动。
3. v1 索引就绪项（默认最多等待 240s，超时则 SKIP 而非 FAIL）：
   - 正则 `blender.*\\.exe` 有真实结果（应包含 D:\\software 下 blender 相关文件）；
   - everything_count 与 everything_search.total 一致；
   - path_filter 生效；
   - 查询耗时（目标 100ms 级），并与递归扫描 D:\\software 做对比。
4. v2 扩展项（SPEC §6 验收清单）：
   - 工具面：13 个工具 + 3 个 MCP 资源；everything_help / status 能力矩阵 / presets；
   - 查询参数：fields / count_only / 分页 next_offset / sort 回退 / path_scope /
     folder_only / file_only / match_path / max_results 钳制 / v1 兼容参数；
   - 真实临时数据（python 创建）：size: 量化语义、summary(ext/dir/type)、
     duplicates(name/name_size/size/hash)、changes 绝对 dm:、batch_stat、grep、
     export(csv/jsonl/efu + offset)、download（落盘/重名/越界/超限）；
   - 边界与降级：非法参数、工作区/下载根越界、rg 缺失、dupe: 不可用回退、
     EV_ALLOW_SIDE_EFFECTS=0 拒绝 reveal；临时数据清理后索引回收；
   - 性能：搜索热 P95、count、summary/duplicates/grep 耗时。
5. 测试**不会**启动或停止 Everything 实例（EV_AUTO_START=0），直接复用本机
   127.0.0.1:8090 上正在运行的默认实例；临时文件全部在 workspace 下创建并清理。

注意：造测试数据必须用 **python** 写入（python/pwsh 写的文件 Everything 才能秒级索引；
DSH `write` 工具创建的文件在 Everything 索引里不可见，直到被其它进程再写一次）。

传输模式：
    EV_TEST_TRANSPORT=auto   默认。优先真实 stdio 子进程；若当前环境禁止创建管道
                             （例如 DSH workspace-write 沙箱），自动回退到 MCP
                             官方内存传输（同一套 JSON-RPC 握手与工具调用）。
    EV_TEST_TRANSPORT=stdio  强制 stdio 子进程（用于非沙箱环境/CI）。
    EV_TEST_TRANSPORT=memory 强制内存传输。

用法：
    python everything-mcp/tests/test_mcp.py

可用环境变量：
    EV_HTTP_PORT        被测服务器端口，默认 8090
    EV_TEST_INDEX_WAIT  等待索引就绪的秒数，默认 240
    EV_TEST_TRANSPORT   auto / stdio / memory
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

REPO = Path(__file__).resolve().parents[1]
WORKSPACE = REPO.parent
SERVER_SCRIPT = REPO / "server.py"
# 内存传输模式需要在测试进程内导入 everything_mcp 包
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
PORT = int(os.environ.get("EV_HTTP_PORT", "8090"))
INDEX_WAIT = float(os.environ.get("EV_TEST_INDEX_WAIT", "240"))
BAD_PORT = int(os.environ.get("EV_TEST_BAD_PORT", "8099"))
TRANSPORT = os.environ.get("EV_TEST_TRANSPORT", "auto").strip().lower()

PASS: list[str] = []
FAIL: list[str] = []
SKIP: list[str] = []
INFO: list[str] = []
MODE = TRANSPORT if TRANSPORT in {"stdio", "memory"} else "auto"


class ToolError(RuntimeError):
    """MCP 工具返回 isError。"""


def ok(name: str, detail: str = "") -> None:
    PASS.append(name)
    print(f"[PASS] {name}" + (f" — {detail}" if detail else ""))


def fail(name: str, detail: str = "") -> None:
    FAIL.append(name)
    print(f"[FAIL] {name}" + (f" — {detail}" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    SKIP.append(name)
    print(f"[SKIP] {name}" + (f" — {detail}" if detail else ""))


def check(name: str, condition: bool, detail: str = "") -> bool:
    (ok if condition else fail)(name, detail)
    return bool(condition)


def env_for(port: int, auto_start: bool = False, **extra: object) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "EV_HTTP_HOST": os.environ.get("EV_HTTP_HOST", "127.0.0.1"),
            "EV_HTTP_PORT": str(port),
            "EV_HTTP_TIMEOUT": "2",
            "EV_AUTO_START": "1" if auto_start else "0",
            "EV_WORKSPACE_ROOT": str(WORKSPACE),
            "EV_EXPORT_ROOT": str(WORKSPACE),
            "EV_DOWNLOAD_ROOT": str(WORKSPACE),
            "EV_LOG_LEVEL": "WARNING",
        }
    )
    # 显式覆盖（测试只读模式/缺失 rg 等场景）
    for key, value in extra.items():
        env[str(key)] = str(value)
    return env


def server_params(port: int) -> StdioServerParameters:
    """以脚本入口 `python server.py` 启动 MCP 服务器（任意 cwd 可用）。"""
    return StdioServerParameters(
        command=sys.executable,
        args=[str(SERVER_SCRIPT)],
        env=env_for(port),
        cwd=str(WORKSPACE),
    )


def module_params(port: int) -> StdioServerParameters:
    """以 `python -m everything_mcp` 启动（cwd 必须是 everything-mcp/）。"""
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "everything_mcp"],
        env=env_for(port),
        cwd=str(REPO),
    )


def _load_server_module():
    """内存模式：导入服务模块并让其按当前 os.environ 重新创建客户端。"""
    import everything_mcp.server as server_module

    server_module._client = None
    return server_module


def _prepare_memory_env(port: int, auto_start: bool = False) -> None:
    os.environ.update(env_for(port, auto_start))


class PipeSession:
    """极简 MCP stdio 客户端：用匿名管道 + 线程收发 JSON-RPC。

    why：mcp SDK 的 asyncio 子进程在 Windows 上用“重叠命名管道”，在受限沙箱
    （例如 DSH workspace-write）里会被拒绝；而 subprocess.PIPE 走匿名管道可用。
    本类实现标准 MCP stdio 传输（每行一条 JSON-RPC 消息），用于在沙箱内也
    能验证真实的 stdio 子进程握手。生产环境的 DSH 使用官方 SDK，不受影响。
    """

    def __init__(self, command: str, args: list[str], env: dict[str, str], cwd: str):
        self._next_id = 1
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: list[str] = []
        self.proc = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            bufsize=0,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            self._lines.put(raw.decode("utf-8", errors="replace"))
        self._lines.put(None)

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            self._stderr.append(raw.decode("utf-8", errors="replace").rstrip())
            if len(self._stderr) > 200:
                del self._stderr[:100]

    def _send(self, payload: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def _wait_for(self, request_id: int, timeout: float = 60.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._lines.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                raise TimeoutError(f"MCP 服务器 {timeout:.0f}s 内未响应") from None
            if line is None:
                stderr_text = "\n".join(self._stderr[-20:])
                raise RuntimeError(f"MCP 服务器进程已退出。stderr：{stderr_text[:1000]}")
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue  # 非协议输出（服务器日志等）忽略
            if message.get("id") == request_id:
                return message
        raise TimeoutError(f"MCP 请求 {request_id} 超时")

    def request(self, method: str, params: dict | None = None) -> dict:
        request_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
        message = self._wait_for(request_id)
        if "error" in message:
            error = message["error"] or {}
            raise ToolError(str(error.get("message") or error))
        return message.get("result") or {}

    def initialize(self) -> dict:
        result = self.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "everything-mcp-test", "version": "0.1"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        return result

    # 与 ClientSession 对齐的最小接口 --------------------------------
    async def list_tools(self):
        result = await asyncio.to_thread(self.request, "tools/list", {})
        return SimpleNamespace(
            tools=[
                SimpleNamespace(name=tool.get("name"), description=tool.get("description"))
                for tool in result.get("tools", [])
            ]
        )

    async def call_tool(self, name: str, arguments: dict | None = None):
        result = await asyncio.to_thread(
            self.request, "tools/call", {"name": name, "arguments": arguments or {}}
        )
        content = [
            SimpleNamespace(text=part.get("text", ""))
            for part in result.get("content", [])
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return SimpleNamespace(
            content=content,
            isError=bool(result.get("isError")),
            structuredContent=result.get("structuredContent"),
        )

    # MCP 资源（stdio JSON-RPC resources/list、resources/read） ----------
    async def list_resources(self):
        result = await asyncio.to_thread(self.request, "resources/list", {})
        return SimpleNamespace(
            resources=[
                SimpleNamespace(
                    uri=item.get("uri"),
                    name=item.get("name"),
                    mimeType=item.get("mimeType"),
                )
                for item in result.get("resources", [])
                if isinstance(item, dict)
            ]
        )

    async def read_resource(self, uri: str):
        result = await asyncio.to_thread(self.request, "resources/read", {"uri": uri})
        return SimpleNamespace(
            contents=[
                SimpleNamespace(
                    uri=item.get("uri"),
                    mimeType=item.get("mimeType"),
                    text=item.get("text", ""),
                )
                for item in result.get("contents", [])
                if isinstance(item, dict)
            ]
        )

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                self.proc.kill()
            except Exception:  # noqa: BLE001
                pass


@asynccontextmanager
async def client_session(
    port: int,
    *,
    auto_start: bool = False,
    entry: str = "script",
    extra_env: dict | None = None,
):
    """打开一个已 initialize 的 MCP 客户端 session。

    entry: "script" = python server.py；"module" = python -m everything_mcp。
    extra_env: 额外环境变量覆盖（例如 EV_ALLOW_SIDE_EFFECTS=0 的只读模式验证）。
    传输优先级：官方 SDK stdio → 匿名管道 stdio（本类）→ MCP 内存传输。
    """
    global MODE
    command = sys.executable
    args = ["-m", "everything_mcp"] if entry == "module" else [str(SERVER_SCRIPT)]
    cwd = str(REPO) if entry == "module" else str(WORKSPACE)
    env = env_for(port, auto_start, **(extra_env or {}))

    if MODE in {"auto", "stdio"}:
        params = StdioServerParameters(command=command, args=args, env=env, cwd=cwd)
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
            if MODE == "auto":
                MODE = "stdio"
            return
        except (PermissionError, OSError) as exc:
            if MODE == "stdio":
                raise
            print(f"[INFO] 官方 SDK stdio 子进程启动失败（{exc}），尝试匿名管道 stdio。")

    if MODE in {"auto", "pipes"}:
        session = None
        try:
            session = PipeSession(command, args, env, cwd)
            await asyncio.to_thread(session.initialize)
        except (OSError, RuntimeError) as exc:
            if session is not None:
                session.close()
            if MODE == "pipes":
                raise
            print(f"[INFO] 匿名管道 stdio 不可用（{exc}），改用 MCP 内存传输。")
        else:
            MODE = "pipes"
            try:
                yield session
            finally:
                session.close()
            return

    # 内存传输：使用与生产完全相同的 FastMCP 服务器对象
    _prepare_memory_env(port, auto_start)
    server_module = _load_server_module()
    async with create_connected_server_and_client_session(server_module._mcp) as session:
        await session.initialize()
        yield session
    if MODE == "auto":
        MODE = "memory"


async def call_json(session: ClientSession, name: str, arguments: dict | None = None) -> dict:
    """调用工具并把结果解析为 JSON dict。"""
    result = await session.call_tool(name, arguments or {})
    text = ""
    for part in result.content or []:
        text += getattr(part, "text", "") or ""
    if getattr(result, "isError", False):
        raise ToolError(text.strip() or f"{name} 返回 isError，但无错误文本")
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        return structured
    if not text.strip():
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolError(f"{name} 返回非 JSON 文本：{text[:300]}") from exc


# ----------------------------------------------------------------------
# Tier A：必须通过（不依赖索引完整）
# ----------------------------------------------------------------------
async def tier_a(session: ClientSession) -> None:
    print("\n== Tier A：基础契约（不依赖索引是否构建完成） ==")

    tools = await session.list_tools()
    names = {tool.name for tool in tools.tools}
    expected = {
        "everything_search",
        "everything_count",
        "everything_status",
        "everything_export",
    }
    check("tools/list 注册 4 个工具", expected <= names, f"实际：{sorted(names)}")
    descriptions = "\n".join((tool.description or "") for tool in tools.tools)
    check(
        "工具描述包含前提条件（HTTP Server 等）",
        "HTTP Server" in descriptions and "127.0.0.1" in descriptions,
        "已包含 HTTP Server 前置说明",
    )

    status = await call_json(session, "everything_status")
    http = status.get("http") or {}
    everything = status.get("everything") or {}
    check("everything_status 返回 HTTP 可达", bool(http.get("reachable")), f"url={http.get('url')}")
    check(
        "everything_status 报告 Everything 正在运行",
        bool(everything.get("running")),
        f"detected_by={everything.get('detected_by')}",
    )
    check(
        "everything_status 返回版本号",
        bool(everything.get("version")),
        str(everything.get("version")),
    )
    INFO.append(
        f"Everything 版本：{everything.get('version')}；"
        f"索引条目数：{(status.get('index') or {}).get('total_items')}"
    )

    search = await call_json(session, "everything_search", {"query": "everything.exe", "max_results": 5})
    required_keys = {"query", "total", "returned", "results", "elapsed_ms", "channel", "truncated"}
    check("everything_search 返回结构完整", required_keys <= set(search), f"keys={sorted(search)}")
    check("everything_search total 为整数", isinstance(search.get("total"), int), str(search.get("total")))
    check("everything_search channel 为 http", search.get("channel") == "http")
    results = search.get("results") or []
    check("everything_search results 为数组且不超过 max_results", isinstance(results, list) and len(results) <= 5)
    if results:
        item = results[0]
        check(
            "结果条目包含 name/path/type",
            isinstance(item.get("name"), str)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("type"), str),
            json.dumps(item, ensure_ascii=False)[:200],
        )
    check(
        "everything_search 结果条数与 returned 一致",
        len(results) == search.get("returned"),
        f"len={len(results)} returned={search.get('returned')}",
    )

    count = await call_json(session, "everything_count", {"query": "everything.exe"})
    check(
        "everything_count 与 everything_search.total 一致",
        isinstance(count.get("total"), int) and count.get("total") == search.get("total"),
        f"count={count.get('total')} search.total={search.get('total')}",
    )

    # 导出（空索引也允许 0 行）+ 越界拒绝
    export_rel = "everything-mcp/dev/export_test.csv"
    export_path = WORKSPACE / export_rel
    try:
        export = await call_json(
            session,
            "everything_export",
            {"query": "everything.exe", "out_csv": export_rel, "max_results": 5},
        )
        check(
            "everything_export 返回 ok 且行数合法",
            bool(export.get("ok")) and isinstance(export.get("rows"), int),
            f"rows={export.get('rows')} path={export.get('path')}",
        )
        check(
            "everything_export 生成 CSV 文件",
            export_path.is_file() and export_path.stat().st_size > 0,
            str(export_path),
        )
    finally:
        export_path.unlink(missing_ok=True)
    try:
        await call_json(session, "everything_export", {"query": "x", "out_csv": r"C:\Windows\evil.csv"})
        fail("everything_export 拒绝导出到工作区外", "未抛出错误")
    except ToolError as exc:
        check(
            "everything_export 拒绝导出到工作区外",
            "工作区外" in str(exc) or "拒绝" in str(exc),
            str(exc)[:200],
        )

    # 查询耗时（Tier A 只要求不异常慢；毫秒级数据在 Tier B 打印）
    t0 = time.perf_counter()
    await call_json(session, "everything_search", {"query": "everything.exe", "max_results": 10})
    wall = (time.perf_counter() - t0) * 1000
    check("everything_search 单次耗时 < 3000ms", wall < 3000, f"wall={wall:.1f}ms")


async def tier_error_path() -> None:
    print("\n== Tier A：错误路径（错误端口 → 可操作错误） ==")
    try:
        async with client_session(BAD_PORT) as session:
            try:
                await call_json(session, "everything_search", {"query": "x"})
                fail("错误端口返回可操作错误", "未抛出错误")
            except ToolError as exc:
                message = str(exc)
                actionable = ("http_server_enabled" in message) or ("HTTP Server" in message)
                check(
                    "错误端口返回可操作错误",
                    actionable and ("127.0.0.1" in message),
                    message.replace("\n", " ")[:400],
                )
    except Exception as exc:  # noqa: BLE001 - 启动/握手失败也属于失败
        fail("错误端口返回可操作错误", f"启动/握手异常：{exc}")


async def tier_entries() -> None:
    print("\n== Tier A：两个入口可用性 ==")
    # 1) python server.py（脚本入口）已由主 session 覆盖
    if MODE in {"stdio", "pipes"}:
        ok("python everything-mcp/server.py 入口可用", f"真实 stdio 握手成功（{MODE}）")
    else:
        # 内存模式无法验证子进程，改为验证入口模块可导入
        result = subprocess.run(
            [sys.executable, "-c", "import everything_mcp.server as s; print(s.__name__)"],
            cwd=str(REPO),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        check("python everything-mcp/server.py 入口可导入", result.returncode == 0)

    # 2) python -m everything_mcp
    if MODE in {"stdio", "pipes"}:
        try:
            async with client_session(PORT, entry="module") as session:
                status = await call_json(session, "everything_status")
                check(
                    "python -m everything_mcp 可握手并返回 status",
                    bool((status.get("http") or {}).get("reachable")),
                    f"url={(status.get('http') or {}).get('url')}",
                )
        except Exception as exc:  # noqa: BLE001
            fail("python -m everything_mcp 可握手并返回 status", f"异常：{exc}")
    else:
        result = subprocess.run(
            [sys.executable, "-c", "from everything_mcp.__main__ import main; print(callable(main))"],
            cwd=str(REPO),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        check("python -m everything_mcp 入口可执行", result.returncode == 0)


# ----------------------------------------------------------------------
# Tier B：索引就绪后的真实查询验证（超时 SKIP）
# ----------------------------------------------------------------------
async def wait_for_index(session: ClientSession, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    last: int | None = None
    while time.monotonic() < deadline:
        try:
            data = await call_json(session, "everything_count", {"query": ""})
            last = data.get("total")
            if isinstance(last, int) and last > 0:
                return last
        except Exception:  # noqa: BLE001 - 索引构建期间允许失败
            pass
        await asyncio.sleep(2)
    return last or 0


def recursive_scan(root: Path, pattern: str) -> tuple[int, float]:
    """递归扫描基准（用于对比 Everything 毫秒级搜索）。"""
    regex = re.compile(pattern, re.IGNORECASE)
    started = time.perf_counter()
    hits = 0
    for _dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            if regex.search(filename):
                hits += 1
    return hits, (time.perf_counter() - started) * 1000


async def tier_b(session: ClientSession) -> None:
    print(f"\n== Tier B：索引就绪验证（最多等待 {INDEX_WAIT:.0f}s） ==")
    total_items = await wait_for_index(session, INDEX_WAIT)
    if total_items <= 0:
        skip("索引就绪", f"等待 {INDEX_WAIT:.0f}s 后索引条目数仍为 0（Everything 仍在构建索引）")
        skip("正则查询 blender.*\\.exe")
        skip("count 与 search.total 一致")
        skip("path_filter 生效")
        skip("毫秒级耗时断言")
        return
    ok("索引就绪", f"索引条目数={total_items}")

    # 预热（第一次调用不用于计时）
    await call_json(session, "everything_search", {"query": "everything.exe", "max_results": 5})

    regex = r"blender.*\.exe"
    search = await call_json(session, "everything_search", {"query": regex, "regex": True, "max_results": 50})
    results = search.get("results") or []
    check(
        "正则查询 blender.*\\.exe 有真实结果",
        search.get("total", 0) > 0 and len(results) > 0,
        f"total={search.get('total')} returned={len(results)}",
    )
    if results:
        names_ok = all(
            "blender" in (item.get("name") or "").lower()
            and (item.get("name") or "").lower().endswith(".exe")
            for item in results
        )
        check(
            "正则结果文件名匹配 blender*.exe",
            names_ok,
            json.dumps([item.get("name") for item in results[:5]], ensure_ascii=False),
        )
        in_software = [
            item["path"] for item in results if (item.get("path") or "").lower().startswith("d:\\software")
        ]
        check(
            "结果包含 D:\\software 下 blender 相关文件",
            bool(in_software),
            json.dumps(in_software[:3], ensure_ascii=False),
        )
        INFO.append(
            f"blender 正则命中 {search.get('total')} 条，示例："
            + ", ".join(item["path"] for item in results[:3])
        )

    count = await call_json(session, "everything_count", {"query": regex, "regex": True})
    check(
        "count 与 search.total 一致（正则）",
        count.get("total") == search.get("total"),
        f"count={count.get('total')} search.total={search.get('total')}",
    )

    filtered = await call_json(
        session,
        "everything_search",
        {"query": "blender", "path_filter": "D:\\software", "max_results": 20},
    )
    filtered_results = filtered.get("results") or []
    check(
        "path_filter 只返回 D:\\software 下的结果",
        len(filtered_results) > 0
        and all((item.get("path") or "").lower().startswith("d:\\software") for item in filtered_results),
        f"returned={len(filtered_results)} total={filtered.get('total')}",
    )

    # 耗时：连续 3 次正则查询
    timings: list[float] = []
    for _ in range(3):
        t0 = time.perf_counter()
        await call_json(session, "everything_search", {"query": regex, "regex": True, "max_results": 100})
        timings.append((time.perf_counter() - t0) * 1000)
    average = sum(timings) / len(timings)
    check(
        "正则查询耗时 < 500ms（目标 100ms 级）",
        average < 500,
        "、".join(f"{value:.1f}ms" for value in timings) + f"；平均 {average:.1f}ms",
    )
    INFO.append("MCP 搜索耗时：平均 %.1fms（%s）" % (average, ", ".join(f"{v:.1f}" for v in timings)))

    # 与递归扫描对比（不计入断言，作为 README 性能数据）
    software = Path(r"D:\software")
    if software.is_dir():
        hits, scan_ms = recursive_scan(software, regex)
        INFO.append(
            f"性能对比：Everything 正则搜索平均 {average:.1f}ms / 命中 {search.get('total')} 条；"
            f"Python 递归扫描 D:\\software 耗时 {scan_ms:.1f}ms / 命中 {hits} 条；"
            f"加速约 {scan_ms / max(average, 0.001):.0f}x"
        )
        print(f"[INFO] {INFO[-1]}")


# ----------------------------------------------------------------------
# v2 扩展测试（SPEC §6 验收清单；v1 断言全部保留）
# ----------------------------------------------------------------------
class TempWorkspace:
    """在 workspace 内用 **python** 造真实测试数据（python 写入的才会被 Everything 索引）。

    每个实例使用唯一 token/目录名；测试后删除并轮询索引归零。
    """

    def __init__(self) -> None:
        seed = uuid.uuid4().hex[:8]
        self.token = f"evt5tok{seed}"
        self.dir_name = f"evt5probe_{os.getpid()}_{seed}"
        self.root = WORKSPACE / self.dir_name
        self.created = False

    def setup(self) -> "TempWorkspace":
        if self.created:
            return self
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("a", "b", "c", "c/sub"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        t = self.token
        # 同名重复对（不同目录、同内容）——duplicates name/name_size/hash
        self.dup_a.write_bytes(b"same-content\n")
        self.dup_b.write_bytes(b"same-content\n")
        # 唯一名
        self.unique.write_bytes(b"unique-content\n")
        # grep 用的真实代码/文本
        self.grep_py.write_text('EVAL_MARKER = "hello-ev-test"\n', encoding="utf-8")
        self.grep_txt.write_text("EVAL_MARKER in text\n", encoding="utf-8")
        # 同大小不同内容（size 分组 / hash 必须区分）
        self.s1.write_bytes(b"A" * 100)
        self.s2.write_bytes(b"B" * 100)
        # 同内容不同名（hash 模式应确认重复）
        self.hash1.write_bytes(b"identical-hash-payload")
        self.hash2.write_bytes(b"identical-hash-payload")
        # 嵌套文件（summary group_by=dir 用）
        self.nested.write_text("nested\n", encoding="utf-8")
        # size: 语义边界（kb 按 ceil(size/1024) 的 KB 量化比较）
        sizes_dir = self.root / "c" / "sizes"
        sizes_dir.mkdir(parents=True, exist_ok=True)
        for name, size in (
            ("sz0", 0), ("sz1", 1), ("sz12", 12), ("sz1024", 1024),
            ("sz1025", 1025), ("sz1500", 1500), ("sz2048", 2048),
        ):
            (sizes_dir / f"{t}_{name}.bin").write_bytes(b"x" * size)
        self.created = True
        return self

    # 常用路径 --------------------------------------------------------
    @property
    def dup_a(self) -> Path:
        return self.root / "a" / f"{self.token}_dup.txt"

    @property
    def dup_b(self) -> Path:
        return self.root / "b" / f"{self.token}_dup.txt"

    @property
    def unique(self) -> Path:
        return self.root / "c" / f"{self.token}_unique.txt"

    @property
    def grep_py(self) -> Path:
        return self.root / "c" / f"{self.token}_grep_a.py"

    @property
    def grep_txt(self) -> Path:
        return self.root / "c" / f"{self.token}_grep_b.txt"

    @property
    def s1(self) -> Path:
        return self.root / "c" / f"{self.token}_s1.bin"

    @property
    def s2(self) -> Path:
        return self.root / "c" / f"{self.token}_s2.bin"

    @property
    def hash1(self) -> Path:
        return self.root / "c" / f"{self.token}_hash1.bin"

    @property
    def hash2(self) -> Path:
        return self.root / "c" / f"{self.token}_hash2.bin"

    @property
    def nested(self) -> Path:
        return self.root / "c" / "sub" / f"{self.token}_nested.txt"

    @property
    def file_count(self) -> int:
        return 17  # 10 基础 + 7 个 size 边界文件

    def scope(self) -> str:
        return str(self.root)

    def size_scope(self) -> str:
        return str(self.root / "c" / "sizes")

    def rel(self, name: str) -> str:
        return f"{self.dir_name}/{name}"

    def cleanup(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)
        self.created = False


async def wait_index_total(session: ClientSession, query: str, expected: int,
                           timeout: float = 45.0) -> int:
    """轮询 everything_count 直到 total == expected（Everything 索引实时更新，1~5s）。"""
    deadline = time.monotonic() + timeout
    total = -1
    while time.monotonic() < deadline:
        try:
            data = await call_json(session, "everything_count", {"query": query})
            total = int(data.get("total") or 0)
        except ToolError:
            total = -1
        if total == expected:
            return total
        await asyncio.sleep(0.5)
    return total


async def list_resource_uris(session: ClientSession) -> list[str]:
    result = await session.list_resources()
    return [str(getattr(item, "uri", "")) for item in getattr(result, "resources", []) or []]


async def read_resource_text(session: ClientSession, uri: str) -> str:
    result = await session.read_resource(uri)
    parts: list[str] = []
    for content in getattr(result, "contents", []) or []:
        text = getattr(content, "text", None)
        if text:
            parts.append(str(text))
    return "\n".join(parts)


async def expect_error(session: ClientSession, name: str, arguments: dict, label: str,
                       keywords: tuple[str, ...] = ()) -> str:
    """断言工具返回 isError 且错误文本包含关键词；返回错误文本。"""
    try:
        await call_json(session, name, arguments)
    except ToolError as exc:
        text = str(exc)
        matched = all(keyword in text for keyword in keywords) if keywords else bool(text.strip())
        check(label, matched, text.replace("\n", " ")[:320])
        return text
    fail(label, "未返回错误（预期 isError）")
    return ""


def group_path_strings(group: dict) -> list[str]:
    """兼容 duplicates 组内 paths 的两种形态：字符串列表 或 {path,size,...} 列表。"""
    result: list[str] = []
    for item in group.get("paths") or []:
        value = item if isinstance(item, str) else (item or {}).get("path")
        if value:
            result.append(str(value))
    return result


@asynccontextmanager
async def memory_session(**env_overrides):
    """进程内 MCP 内存传输 session（用于 monkeypatch 降级路径验证）。"""
    import everything_mcp.server as server_module

    saved = os.environ.copy()
    try:
        os.environ.update(env_for(PORT))
        os.environ.update({str(key): str(value) for key, value in env_overrides.items()})
        server_module._client = None
        async with create_connected_server_and_client_session(server_module._mcp) as session:
            await session.initialize()
            yield session
    finally:
        os.environ.clear()
        os.environ.update(saved)
        try:
            server_module._client = None
        except Exception:  # noqa: BLE001
            pass


async def tier_v2_surface(session: ClientSession) -> None:
    print("\n== v2：工具面 / help / status / presets / MCP 资源 ==")
    tools = await session.list_tools()
    names = {tool.name for tool in tools.tools}
    expected = {
        "everything_search", "everything_count", "everything_status", "everything_help",
        "everything_presets", "everything_export", "everything_download",
        "everything_summary", "everything_duplicates", "everything_grep",
        "everything_batch_stat", "everything_changes", "everything_reveal",
    }
    check("v2 tools/list 注册 13 个工具", expected <= names,
          f"缺失：{sorted(expected - names)}")

    help_data = await call_json(session, "everything_help")
    required = {
        "version", "verified_everything_version", "syntax_supported",
        "syntax_unsupported", "slow_queries", "http_params", "tools", "examples",
        "tips", "resource_uris",
    }
    check("everything_help 返回结构化能力矩阵", required <= set(help_data),
          f"keys={sorted(help_data)[:12]}")
    help_tools = {item.get("name") for item in help_data.get("tools") or []}
    check("everything_help 覆盖全部 13 工具", expected <= help_tools,
          f"缺失：{sorted(expected - help_tools)}")
    blob = json.dumps(help_data, ensure_ascii=False)
    check("everything_help 写明 dupe:/size/慢查询",
          "dupe" in blob and "size" in blob and "dc:" in blob)
    slow_text = json.dumps(help_data.get("slow_queries") or [], ensure_ascii=False)
    check("everything_help 慢查询含 dc:/da:/attrib:",
          "dc:" in slow_text and "da:" in slow_text and "attrib:" in slow_text,
          slow_text[:160])

    status = await call_json(session, "everything_status")
    check("v2 status 含 DB/索引开关/卷",
          {"index_db", "index_switches", "volumes"} <= set(status))
    switches = status.get("index_switches") or {}
    check("v2 status 报告三项索引开关",
          {"index_date_created", "index_date_accessed", "index_attributes"} <= set(switches),
          json.dumps(switches, ensure_ascii=False)[:200])
    rg_info = status.get("rg") or {}
    check("v2 status 报告 rg 可用", bool(rg_info.get("available")) and bool(rg_info.get("path")),
          str(rg_info.get("path")))
    capabilities = status.get("capabilities") or {}
    check("v2 status 含能力矩阵摘要", "size_semantics" in capabilities and "dupe" in capabilities,
          f"keys={sorted(capabilities)[:10]}")
    presets_info = status.get("presets") or {}
    check("v2 status 报告预设数量（>=8）",
          isinstance(presets_info.get("count"), int) and presets_info.get("count") >= 8,
          str(presets_info.get("count")))
    check("v2 status 索引条目数 > 0", (status.get("index") or {}).get("total_items", 0) > 0,
          str((status.get("index") or {}).get("total_items")))

    presets = await call_json(session, "everything_presets", {"action": "list"})
    preset_names = {item.get("name") for item in presets.get("presets") or []}
    builtin = {
        "recent_modified_7d", "today_changed", "large_files_100mb", "screenshots_7d",
        "archives_100mb", "git_repos", "node_modules", "downloads_today",
    }
    check("everything_presets list 返回 8 个内置预设", builtin <= preset_names,
          f"缺失：{sorted(builtin - preset_names)}")

    uris = await list_resource_uris(session)
    expected_uris = {"everything://help/syntax", "everything://status", "everything://presets"}
    check("MCP 资源注册 3 个", expected_uris <= set(uris), f"实际：{uris}")
    help_md = await read_resource_text(session, "everything://help/syntax")
    check("资源 everything://help/syntax 返回 markdown",
          len(help_md) > 200 and "#" in help_md, f"len={len(help_md)}")
    try:
        status_obj = json.loads(await read_resource_text(session, "everything://status"))
    except (json.JSONDecodeError, ToolError):
        status_obj = {}
    check("资源 everything://status 返回合法 JSON",
          isinstance(status_obj, dict) and "http" in status_obj)
    try:
        presets_obj = json.loads(await read_resource_text(session, "everything://presets"))
    except (json.JSONDecodeError, ToolError):
        presets_obj = {}
    check("资源 everything://presets 返回预设清单",
          isinstance(presets_obj, dict) and "count" in presets_obj)


async def tier_v2_query(session: ClientSession) -> None:
    print("\n== v2：search 参数（fields/count_only/分页/排序/path_scope/类型/兼容） ==")
    fields_search = await call_json(
        session, "everything_search", {"query": "ext:exe", "max_results": 5, "fields": ["name"]}
    )
    check("fields 裁剪只返回 name/type",
          bool(fields_search.get("results"))
          and all(set(item) <= {"name", "type"} for item in fields_search.get("results") or []),
          json.dumps((fields_search.get("results") or [])[:1], ensure_ascii=False))
    check("fields 未请求时不下发 path 列",
          all("path" not in item for item in fields_search.get("results") or []))

    count_only = await call_json(
        session, "everything_search", {"query": "ext:exe", "count_only": True, "max_results": 5}
    )
    check("count_only 只回 total（results 为空）",
          count_only.get("results") == [] and count_only.get("returned") == 0
          and count_only.get("total", 0) > 0,
          f"total={count_only.get('total')} returned={count_only.get('returned')}")

    page1 = await call_json(
        session, "everything_search",
        {"query": "ext:exe", "max_results": 5, "offset": 0, "sort": "name",
         "ascending": True, "fields": ["name", "path"]},
    )
    page2 = await call_json(
        session, "everything_search",
        {"query": "ext:exe", "max_results": 5, "offset": page1.get("next_offset", 0),
         "sort": "name", "ascending": True, "fields": ["name", "path"]},
    )
    check("分页 next_offset 正确推进",
          page1.get("next_offset") == 5 and page2.get("offset") == 5,
          f"next={page1.get('next_offset')} offset2={page2.get('offset')}")
    page1_keys = {(item.get("name"), item.get("path")) for item in page1.get("results") or []}
    page2_keys = {(item.get("name"), item.get("path")) for item in page2.get("results") or []}
    check("分页页间不重叠", len(page1_keys | page2_keys) == len(page1_keys) + len(page2_keys),
          f"p1={len(page1_keys)} p2={len(page2_keys)}")

    bad_sort = await call_json(
        session, "everything_search", {"query": "ext:exe", "max_results": 3, "sort": "date_created"}
    )
    check("不支持的 sort 回退 name 并告警",
          bad_sort.get("sort") == "name"
          and any("sort" in warning for warning in bad_sort.get("warnings") or []),
          json.dumps(bad_sort.get("warnings"), ensure_ascii=False)[:200])

    bad_fields = await call_json(
        session, "everything_search",
        {"query": "everything.exe", "max_results": 2, "fields": ["date_created"]},
    )
    check("不支持的 fields 进 warnings 并忽略",
          any("fields" in warning for warning in bad_fields.get("warnings") or [])
          and all("date_created" not in item for item in bad_fields.get("results") or []),
          json.dumps(bad_fields.get("warnings"), ensure_ascii=False)[:200])

    scoped = await call_json(
        session, "everything_search",
        {"query": "blender", "path_scope": r"D:\software", "max_results": 10},
    )
    check("path_scope 只返回该前缀下结果",
          scoped.get("returned", 0) > 0
          and all(str(item.get("path", "")).lower().startswith("d:\\software")
                  for item in scoped.get("results") or []),
          f"returned={scoped.get('returned')}")
    check("path_scope 组合进 effective_query",
          'path:"' in (scoped.get("effective_query") or ""), str(scoped.get("effective_query")))

    folder_only = await call_json(
        session, "everything_search", {"query": "blender", "folder_only": True, "max_results": 10}
    )
    check("folder_only 只返回目录",
          folder_only.get("returned", 0) > 0
          and all(item.get("type") == "folder" for item in folder_only.get("results") or []),
          f"returned={folder_only.get('returned')}")
    file_only = await call_json(
        session, "everything_search", {"query": "blender", "file_only": True, "max_results": 10}
    )
    check("file_only 只返回文件",
          file_only.get("returned", 0) > 0
          and all(item.get("type") == "file" for item in file_only.get("results") or []),
          f"returned={file_only.get('returned')}")

    match_path = await call_json(
        session, "everything_search", {"query": "blender", "match_path": True, "max_results": 5}
    )
    plain = await call_json(
        session, "everything_search", {"query": "blender", "max_results": 5}
    )
    check("match_path 扩大命中范围（path=1 match-path 开关）",
          match_path.get("total", 0) > plain.get("total", 0) * 5,
          f"match_path={match_path.get('total')} plain={plain.get('total')}")

    cap = await call_json(
        session, "everything_search", {"query": "ext:exe", "max_results": 5000}
    )
    check("max_results 超上限被钳制到 1000",
          cap.get("max_results") == 1000 and cap.get("returned", 0) <= 1000,
          f"max_results={cap.get('max_results')} returned={cap.get('returned')}")

    legacy = await call_json(
        session, "everything_search",
        {"query": "blender", "path_filter": r"D:\software", "columns": ["size"],
         "max_results": 5},
    )
    check("v1 兼容参数 path_filter/columns 仍可用并告警",
          any("columns" in warning for warning in legacy.get("warnings") or [])
          and all(item.get("type") != "file" or "size" in item
                  for item in legacy.get("results") or []),
          json.dumps(legacy.get("warnings"), ensure_ascii=False)[:200])

    count_scoped = await call_json(
        session, "everything_count", {"query": "blender", "path_scope": r"D:\software"}
    )
    check("count 支持 path_scope 且与 search.total 一致",
          count_scoped.get("total", 0) > 0
          and count_scoped.get("total") == scoped.get("total"),
          f"count={count_scoped.get('total')} search={scoped.get('total')}")

    approx = await call_json(
        session, "everything_search",
        {"query": r".*\.py", "regex": True,
         "path_scope": str(REPO), "max_results": 5},
    )
    check("regex+path_scope 走客户端过滤并告警",
          bool(approx.get("warnings")) and approx.get("approx") is True
          and all(str(item.get("path", "")).lower().startswith(str(REPO).lower())
                  for item in approx.get("results") or []),
          json.dumps(approx.get("warnings"), ensure_ascii=False)[:200])

    date_item = ((await call_json(
        session, "everything_search",
        {"query": "everything.exe", "max_results": 3,
         "fields": ["name", "size", "date_modified"]},
    )).get("results") or [{}])[0]
    check("v2 日期归一化为 ISO 8601 UTC",
          bool(re.match(r"^\d{4}-\d{2}-\d{2}T", str(date_item.get("date_modified", ""))))
          and str(date_item.get("date_modified", "")).endswith("Z"),
          str(date_item.get("date_modified")))
    check("v2 size 为整数", isinstance(date_item.get("size"), int), str(date_item.get("size")))

    last_hour = await call_json(session, "everything_count", {"query": "dm:last1hour"})
    check("dm:last1hour 在 1.4 返回 0（changes 需换算绝对时间）",
          last_hour.get("total") == 0, f"total={last_hour.get('total')}")


async def tier_v2_temp(session: ClientSession, workspace: TempWorkspace) -> None:
    print("\n== v2：真实临时数据（索引/分页/size 语义/summary/duplicates/changes/...） ==")
    total = await wait_index_total(session, workspace.token, workspace.file_count)
    if total != workspace.file_count:
        fail("临时数据被 Everything 实时索引",
             f"token 命中 {total} 条，期望 {workspace.file_count} 条（Everything 索引延迟？）")
        for label in (
            "临时数据搜索/path_scope", "size: 语义断言", "summary(ext/dir/type)",
            "summary(dir, D:\\software) size>0", "duplicates(name/name_size/size/hash)",
            "duplicates(name_size) 输出体积可控", "changes 绝对 dm: 过滤",
            "batch_stat", "grep", "export 三格式", "download",
        ):
            skip(label, "临时数据未进索引，跳过")
        return
    ok("临时数据被 Everything 实时索引", f"{total} 条")
    scope = workspace.scope()

    found = await call_json(
        session, "everything_search",
        {"query": workspace.token, "path_scope": scope, "sort": "name", "ascending": True,
         "max_results": 30, "fields": ["name", "path", "size"]},
    )
    check("临时数据搜索命中全部文件",
          found.get("total") == workspace.file_count
          and found.get("returned") == workspace.file_count,
          f"total={found.get('total')} returned={found.get('returned')}")
    check("临时数据 path_scope 全部落在临时目录",
          all(str(item.get("path", "")).lower().startswith(scope.lower())
              for item in found.get("results") or []))

    p1 = await call_json(
        session, "everything_search",
        {"query": workspace.token, "path_scope": scope, "max_results": 5, "offset": 0,
         "sort": "name", "ascending": True},
    )
    p2 = await call_json(
        session, "everything_search",
        {"query": workspace.token, "path_scope": scope, "max_results": 5,
         "offset": p1.get("next_offset", 0), "sort": "name", "ascending": True},
    )
    p3 = await call_json(
        session, "everything_search",
        {"query": workspace.token, "path_scope": scope, "max_results": 5,
         "offset": p2.get("next_offset", 0), "sort": "name", "ascending": True},
    )
    page_keys = [
        {(item.get("name"), item.get("path")) for item in page.get("results") or []}
        for page in (p1, p2, p3)
    ]
    check("临时数据分页 3 页不重叠且 next_offset 递进",
          sum(len(keys) for keys in page_keys) == 15
          and len(set().union(*page_keys)) == 15
          and p1.get("next_offset") == 5 and p2.get("next_offset") == 10,
          f"sizes={[len(k) for k in page_keys]} offsets="
          f"{[p.get('next_offset') for p in (p1, p2, p3)]}")

    # size: 语义（kb 按 ceil(size/1024) 的 KB 量化；无单位/b 精确字节）
    # 该子目录只放 0/1/12/1024/1025/1500/2048 字节 7 个文件，便于精确断言。
    size_scope = workspace.size_scope()

    def size_total(data: dict) -> int:
        return int(data.get("total") or 0)

    sz_1kb = await call_json(session, "everything_search",
                             {"query": f'size:1kb path:"{size_scope}"', "count_only": True})
    sz_2kb = await call_json(session, "everything_search",
                             {"query": f'size:2kb path:"{size_scope}"', "count_only": True})
    sz_lt1kb = await call_json(session, "everything_search",
                               {"query": f'size:<1kb path:"{size_scope}"', "count_only": True})
    sz_1k = await call_json(session, "everything_search",
                            {"query": f'size:1k path:"{size_scope}"', "count_only": True})
    sz_1024b = await call_json(session, "everything_search",
                               {"query": f'size:1024b path:"{size_scope}"', "count_only": True})
    sz_1mb = await call_json(session, "everything_search",
                             {"query": f'size:1mb path:"{size_scope}"', "count_only": True})
    sz_range = await call_json(session, "everything_search",
                               {"query": f'size:1..1024 path:"{size_scope}"', "count_only": True})
    check("size:1kb == ceil(size/1024)==1（1/12/1024B 共 3 条）",
          size_total(sz_1kb) == 3, f"total={size_total(sz_1kb)}")
    check("size:2kb == ceil==2（1025/1500/2048B 共 3 条）",
          size_total(sz_2kb) == 3, f"total={size_total(sz_2kb)}")
    check("size:<1kb 只命中 0 字节（1 条）",
          size_total(sz_lt1kb) == 1, f"total={size_total(sz_lt1kb)}")
    check("size:1k（无 b）按精确字节 == size:1",
          size_total(sz_1k) == 1, f"total={size_total(sz_1k)}")
    check("size:1024b 精确字节（1 条）",
          size_total(sz_1024b) == 1, f"total={size_total(sz_1024b)}")
    check("size:1mb 是 KB 量化窗口（本临时集无 1MB 文件 → 0 条）",
          size_total(sz_1mb) == 0, f"total={size_total(sz_1mb)}")
    check("size:1..1024（无单位精确区间）= 1/12/1024B 共 3 条",
          size_total(sz_range) == 3, f"total={size_total(sz_range)}")

    summary = await call_json(
        session, "everything_summary",
        {"query": workspace.token, "path_scope": scope, "group_by": "ext",
         "top_n": 20, "max_scan": 1000},
    )
    groups = {group.get("key"): group for group in summary.get("groups") or []}
    check("summary(ext) 扫描完整（scanned=17）",
          summary.get("total") == workspace.file_count
          and summary.get("scanned") == workspace.file_count
          and not summary.get("truncated_scan"),
          f"total={summary.get('total')} scanned={summary.get('scanned')}")
    check("summary(ext) 分组计数正确（txt=5/py=1/bin=11）",
          groups.get("txt", {}).get("count") == 5
          and groups.get("py", {}).get("count") == 1
          and groups.get("bin", {}).get("count") == 11,
          json.dumps({k: v.get("count") for k, v in groups.items()}, ensure_ascii=False))
    check("summary totals.size > 0", (summary.get("totals") or {}).get("size", 0) > 0,
          str((summary.get("totals") or {}).get("size")))

    summary_dir = await call_json(
        session, "everything_summary",
        {"query": workspace.token, "path_scope": scope, "group_by": "dir",
         "dir_depth": 2, "top_n": 20},
    )
    check("summary(dir) 按目录分组（>=3 组）",
          len(summary_dir.get("groups") or []) >= 3,
          f"groups={len(summary_dir.get('groups') or [])}")
    summary_type = await call_json(
        session, "everything_summary",
        {"query": workspace.token, "path_scope": scope, "group_by": "type", "top_n": 10},
    )
    type_groups = summary_type.get("groups") or []
    check("summary(type) 全部归入 file",
          len(type_groups) == 1 and type_groups[0].get("key") == "file"
          and type_groups[0].get("count") == workspace.file_count,
          json.dumps(type_groups, ensure_ascii=False)[:160])

    # Lead 联调要求：真实目录 group_by=dir 的 totals.size 必须 > 0
    summary_real = await call_json(
        session, "everything_summary",
        {"group_by": "dir", "path_scope": r"D:\software", "max_scan": 2000, "top_n": 20},
    )
    real_groups = summary_real.get("groups") or []
    check("summary(dir, D:\\software) totals.size > 0",
          (summary_real.get("totals") or {}).get("size", 0) > 0,
          f"totals={json.dumps(summary_real.get('totals'), ensure_ascii=False)}")
    check("summary 默认只聚合文件（include_folders=false）",
          summary_real.get("include_folders") is False
          and summary_real.get("aggregates") == "files",
          f"include_folders={summary_real.get('include_folders')} "
          f"aggregates={summary_real.get('aggregates')}")
    summary_folders = await call_json(
        session, "everything_summary",
        {"group_by": "dir", "path_scope": r"D:\software", "max_scan": 800,
         "include_folders": True, "top_n": 20},
    )
    check("summary include_folders=true 额外聚合目录",
          summary_folders.get("aggregates") == "files+folders"
          and (summary_folders.get("totals") or {}).get("count", 0) > 0,
          f"aggregates={summary_folders.get('aggregates')} "
          f"totals={json.dumps(summary_folders.get('totals'), ensure_ascii=False)}")
    check("summary(dir) 每组 size>=0 且至少一组 size>0",
          all(int(group.get("size") or 0) >= 0 for group in real_groups)
          and any(int(group.get("size") or 0) > 0 for group in real_groups),
          f"groups={len(real_groups)}")

    dups = await call_json(
        session, "everything_duplicates",
        {"mode": "name", "query": workspace.token, "path_scope": scope, "max_files": 500},
    )
    dup_groups = dups.get("groups") or []
    dup_stats = dups.get("stats") or {}
    name_group = next((group for group in dup_groups if group.get("count") == 2), None)
    name_paths = group_path_strings(name_group) if name_group else []
    check("duplicates(name) 找到同名重复组（2 条路径）",
          name_group is not None and len(name_paths) == 2
          and {str(Path(path)) for path in name_paths}
          == {str(workspace.dup_a), str(workspace.dup_b)},
          json.dumps(dup_groups, ensure_ascii=False)[:200])
    check("duplicates(name) 使用 dupe: 或明确回退",
          dup_stats.get("dupe_support") is True or dup_stats.get("dupe_fallback") is True,
          json.dumps({k: dup_stats.get(k) for k in
                      ("dupe_support", "dupe_fallback", "dupe_query_used")},
                     ensure_ascii=False))

    dups_name_size = await call_json(
        session, "everything_duplicates",
        {"mode": "name_size", "query": workspace.token, "path_scope": scope,
         "max_files": 500},
    )
    check("duplicates(name_size) 找到同名同大小组",
          any(group.get("count") == 2 for group in dups_name_size.get("groups") or []),
          json.dumps(dups_name_size.get("groups"), ensure_ascii=False)[:200])

    dups_size = await call_json(
        session, "everything_duplicates",
        {"mode": "size", "query": workspace.token, "path_scope": scope, "max_files": 500},
    )
    size_groups = dups_size.get("groups") or []
    check("duplicates(size) 按同大小分组（>=3 组，每组>=2）",
          len(size_groups) >= 3 and all(int(group.get("count") or 0) >= 2 for group in size_groups),
          f"groups={len(size_groups)}")

    dups_hash = await call_json(
        session, "everything_duplicates",
        {"mode": "hash", "query": workspace.token, "path_scope": scope,
         "max_files": 500, "hash_algo": "sha256", "max_hash_bytes": 10**6},
    )
    hash_groups = dups_hash.get("groups") or []
    check("duplicates(hash) 确认同内容重复（>=2 组）",
          len(hash_groups) >= 2 and all(group.get("hash") for group in hash_groups),
          f"groups={len(hash_groups)}")
    s1_key, s2_key = str(workspace.s1).lower(), str(workspace.s2).lower()
    def group_has_both(group: dict) -> bool:
        paths = {path.lower() for path in group_path_strings(group)}
        return s1_key in paths and s2_key in paths

    check("duplicates(hash) 区分同大小不同内容（s1/s2 不同组）",
          not any(group_has_both(group) for group in hash_groups))
    digest = hashlib.sha256(workspace.hash1.read_bytes()).hexdigest()
    check("duplicates(hash) 组内哈希与本地 sha256 一致",
          any(group.get("hash") == digest for group in hash_groups),
          f"expected={digest[:12]}")

    # Lead 联调要求：真实目录 name_size 输出体积可控（默认每组件路径截断 + 体积 <4KB）
    dups_real = await call_json(
        session, "everything_duplicates",
        {"mode": "name_size", "path_scope": r"D:\software"},
    )
    real_dup_groups = dups_real.get("groups") or []
    real_dup_stats = dups_real.get("stats") or {}
    real_bytes = len(json.dumps(dups_real, ensure_ascii=False).encode("utf-8"))
    max_paths_per_group = int(real_dup_stats.get("max_paths_per_group") or 0)
    check("duplicates(name_size, D:\\software) 响应 JSON < 4KB",
          real_bytes < 4096, f"bytes={real_bytes} groups={len(real_dup_groups)}")
    check("duplicates(name_size) 默认体积/组数/路径数受控",
          real_dup_stats.get("max_groups") == 10
          and max_paths_per_group == 2
          and real_dup_stats.get("scan_order") == "size desc"
          and int(real_dup_stats.get("max_response_bytes") or 0) == 4096
          and int(real_dup_stats.get("payload_bytes") or real_bytes) < 4096,
          json.dumps({k: real_dup_stats.get(k) for k in
                      ("max_groups", "max_paths_per_group", "scan_order",
                       "max_response_bytes", "payload_bytes", "payload_truncated")},
                     ensure_ascii=False))
    check("duplicates(name_size) 每组路径截断可控且有 path_count/paths_truncated",
          0 < max_paths_per_group <= 5
          and all(len(group.get("paths") or []) <= max_paths_per_group
                  and "path_count" in group and "paths_truncated" in group
                  for group in real_dup_groups),
          f"groups={len(real_dup_groups)} max_paths_per_group={max_paths_per_group}")

    changes = await call_json(
        session, "everything_changes",
        {"since": "30m", "path_scope": scope, "max_results": 100},
    )
    check("changes(30m) 返回刚创建的临时文件",
          changes.get("count") == workspace.file_count,
          f"count={changes.get('count')} total={changes.get('total')}")
    check("changes 使用绝对 dm:>= 过滤（非 last1hour 相对语法）",
          "dm:>=" in str(changes.get("query") or ""),
          str(changes.get("query")))
    check("changes 对相对 since 给出归一化 warnings",
          any("dm:>=" in str(warning) or "绝对" in str(warning)
              for warning in changes.get("warnings") or []),
          json.dumps(changes.get("warnings"), ensure_ascii=False)[:200])

    stat = await call_json(
        session, "everything_batch_stat",
        {"paths": [str(workspace.dup_a), str(workspace.s1),
                   str(workspace.root / "missing_evt5.bin")],
         "include_hash": True, "hash_algo": "sha256", "max_hash_bytes": 10**6},
    )
    stat_items = stat.get("items") or []
    stat_by_path = {str(item.get("path", "")).lower(): item for item in stat_items}
    dup_stat = stat_by_path.get(str(workspace.dup_a).lower()) or {}
    check("batch_stat 区分存在/缺失",
          stat.get("count") == 3 and stat.get("exists_count") == 2
          and stat.get("missing_count") == 1,
          f"count={stat.get('count')} exists={stat.get('exists_count')}")
    check("batch_stat 返回正确 size/hash/时间",
          dup_stat.get("size") == workspace.dup_a.stat().st_size
          and dup_stat.get("hash") == hashlib.sha256(workspace.dup_a.read_bytes()).hexdigest()
          and str(dup_stat.get("date_modified", "")).endswith("Z"),
          json.dumps(dup_stat, ensure_ascii=False)[:200])
    missing_stat = stat_by_path.get(str(workspace.root / "missing_evt5.bin").lower()) or {}
    check("batch_stat 缺失项 error 可操作",
          missing_stat.get("exists") is False and "不存在" in str(missing_stat.get("error")),
          str(missing_stat.get("error")))

    grepped = await call_json(
        session, "everything_grep",
        {"pattern": "EVAL_MARKER", "path_scope": scope, "max_results": 50,
         "timeout_ms": 10000},
    )
    grep_matches = grepped.get("matches") or []
    grep_stats = grepped.get("stats") or {}
    check("grep 在真实代码/文本中命中（>=2 个文件）",
          len(grep_matches) >= 2 and grep_stats.get("files_matched", 0) >= 2,
          f"matches={len(grep_matches)} files={grep_stats.get('files_matched')}")
    check("grep 返回 path/line/column/text",
          all({"path", "line", "column", "text"} <= set(match) for match in grep_matches[:3]),
          json.dumps(grep_matches[:1], ensure_ascii=False)[:200])
    check("grep 使用真实 rg.exe", bool(grep_stats.get("rg_exe")), str(grep_stats.get("rg_exe")))

    grepped_fixed = await call_json(
        session, "everything_grep",
        {"pattern": "EVAL_MARKER = \"hello", "fixed_strings": True, "path_scope": scope,
         "exts": ["py"], "max_results": 10},
    )
    fixed_matches = grepped_fixed.get("matches") or []
    check("grep fixed_strings+exts 生效",
          len(fixed_matches) >= 1
          and all(str(match.get("path", "")).lower().endswith(".py")
                  for match in fixed_matches),
          f"matches={len(fixed_matches)}")
    grepped_cap = await call_json(
        session, "everything_grep",
        {"pattern": "EVAL_MARKER", "path_scope": scope, "max_results": 5000},
    )
    check("grep max_results 钳制到 1000",
          int((grepped_cap.get("stats") or {}).get("max_results", 0)) <= 1000,
          str((grepped_cap.get("stats") or {}).get("max_results")))

    # 导出三格式
    for fmt, filename in (("csv", "export.csv"), ("jsonl", "export.jsonl"),
                          ("efu", "export.efu")):
        exported = await call_json(
            session, "everything_export",
            {"query": workspace.token, "out_path": workspace.rel(filename),
             "format": fmt, "max_results": 5, "path_scope": scope},
        )
        out_file = WORKSPACE / workspace.rel(filename)
        content = out_file.read_text(encoding="utf-8-sig") if out_file.is_file() else ""
        if fmt == "csv":
            check("export(csv) 写文件且含表头",
                  exported.get("ok") is True and exported.get("rows", 0) > 0
                  and "path" in content.splitlines()[0],
                  f"rows={exported.get('rows')} bytes={exported.get('bytes')}")
        elif fmt == "jsonl":
            lines = [line for line in content.splitlines() if line.strip()]
            check("export(jsonl) 每行合法 JSON",
                  exported.get("ok") is True and len(lines) >= 1
                  and all(isinstance(json.loads(line), dict) for line in lines),
                  f"rows={exported.get('rows')}")
        else:
            check("export(efu) 含 Everything 文件列表表头",
                  exported.get("ok") is True and "Filename" in content.splitlines()[0],
                  f"rows={exported.get('rows')}")
    exported_offset = await call_json(
        session, "everything_export",
        {"query": workspace.token, "out_path": workspace.rel("export_offset.csv"),
         "format": "csv", "max_results": 3, "offset": 4, "path_scope": scope},
    )
    check("export 支持 offset 分页导出",
          exported_offset.get("ok") is True and exported_offset.get("offset") == 4
          and exported_offset.get("rows") == 3,
          f"offset={exported_offset.get('offset')} rows={exported_offset.get('rows')}")

    # 下载（HTTP 文件服务）
    downloaded = await call_json(
        session, "everything_download",
        {"path": str(workspace.hash1), "dest": workspace.rel("downloaded.bin")},
    )
    saved = Path(str(downloaded.get("saved_path") or ""))
    check("download 落盘且字节/内容与源一致",
          saved.is_file() and downloaded.get("ok") is True
          and downloaded.get("bytes") == workspace.hash1.stat().st_size
          and saved.read_bytes() == workspace.hash1.read_bytes(),
          f"saved={saved} bytes={downloaded.get('bytes')}")
    downloaded_again = await call_json(
        session, "everything_download",
        {"path": str(workspace.hash1), "dest": workspace.rel("downloaded.bin")},
    )
    check("download 目标重名自动加序号",
          str(downloaded_again.get("saved_path")) != str(saved)
          and Path(str(downloaded_again.get("saved_path"))).is_file(),
          str(downloaded_again.get("saved_path")))


async def tier_v2_boundaries(session: ClientSession, workspace: TempWorkspace) -> None:
    print("\n== v2：边界与错误路径 ==")
    scope = workspace.scope()
    await expect_error(session, "everything_presets", {"action": "run", "name": "no_such_preset"},
                       "presets 未知预设报错", ("未知预设",))
    await expect_error(session, "everything_presets", {"action": "bogus"},
                       "presets 非法 action 报错", ("不支持的 action",))
    await expect_error(session, "everything_presets", {"action": "run"},
                       "presets run 缺 name 报错", ("必须提供 name",))
    await expect_error(session, "everything_export", {"query": "x", "out_path": workspace.rel("x.xlsx"),
                                                      "format": "xlsx"},
                       "export 非法格式报错", ("不支持的导出格式",))
    await expect_error(session, "everything_export",
                       {"query": "x", "out_path": r"C:\Windows\evt5_evil.csv"},
                       "export 拒绝工作区外路径", ("拒绝导出",))
    await expect_error(session, "everything_export", {"query": "x"},
                       "export 缺少目标路径报错", ("缺少导出目标路径",))
    await expect_error(session, "everything_download",
                       {"path": str(workspace.dup_a), "dest": r"C:\Windows\evt5_evil.bin"},
                       "download 拒绝下载根目录外路径", ("拒绝下载",))
    await expect_error(session, "everything_download",
                       {"path": str(workspace.root / "not_exists.bin")},
                       "download 源文件不存在报错", ("文件不存在",))
    await expect_error(session, "everything_download", {"path": scope},
                       "download 拒绝目录路径", ("是目录",))
    await expect_error(session, "everything_download",
                       {"path": str(workspace.hash1), "max_bytes": 5},
                       "download 超过 max_bytes 报错", ("上限",))
    await expect_error(session, "everything_duplicates", {"mode": "bogus"},
                       "duplicates 非法 mode 报错", ("不支持的 mode",))
    await expect_error(session, "everything_duplicates",
                       {"mode": "name", "hash_algo": "bogus"},
                       "duplicates 非法 hash_algo 报错", ("不支持的 hash_algo",))
    await expect_error(session, "everything_summary", {"group_by": "bogus"},
                       "summary 非法 group_by 报错", ("不支持的 group_by",))
    await expect_error(session, "everything_grep",
                       {"pattern": "x", "path_scope": scope, "max_filesize": "bogus"},
                       "grep 非法 max_filesize 报错", ("格式不正确",))
    await expect_error(session, "everything_grep", {"pattern": "x"},
                       "grep 无搜索根报错", ("无法确定搜索范围",))
    await expect_error(session, "everything_grep",
                       {"pattern": "x", "path_scope": str(workspace.root / "no_dir")},
                       "grep 搜索根不存在报错", ("不存在",))
    await expect_error(session, "everything_batch_stat", {"paths": []},
                       "batch_stat 空 paths 报错", ("paths 不能为空",))
    await expect_error(session, "everything_batch_stat",
                       {"paths": [str(workspace.dup_a)], "hash_algo": "bogus"},
                       "batch_stat 非法 hash_algo 报错", ("不支持的 hash_algo",))
    await expect_error(session, "everything_changes", {"since": "not-a-date"},
                       "changes 非法 since 报错", ("无法解析 since",))
    await expect_error(session, "everything_changes", {"sort": "bogus"},
                       "changes 非法 sort 报错", ("不支持的 sort",))
    await expect_error(session, "everything_reveal",
                       {"path": str(workspace.root / "no_such_reveal.txt")},
                       "reveal 不存在的路径报错（不启动 explorer）", ("路径不存在",))

    many_paths = [str(workspace.root / f"missing_{index}.bin") for index in range(205)]
    truncated = await call_json(session, "everything_batch_stat", {"paths": many_paths})
    check("batch_stat 超过 200 条时截断并告警",
          truncated.get("count") == 200
          and any("200" in str(warning) for warning in truncated.get("warnings") or []),
          f"count={truncated.get('count')}")


async def tier_side_effects_zero(workspace: TempWorkspace) -> None:
    print("\n== v2：EV_ALLOW_SIDE_EFFECTS=0 只读模式（reveal 拒绝） ==")
    try:
        async with client_session(PORT, extra_env={"EV_ALLOW_SIDE_EFFECTS": "0"}) as session:
            result = await call_json(
                session, "everything_reveal", {"path": str(workspace.dup_a)}
            )
            check("reveal 在只读模式被拒绝（ok=false）",
                  result.get("ok") is False and result.get("opened") is False,
                  json.dumps(result, ensure_ascii=False)[:200])
            check("reveal 拒绝信息说明 EV_ALLOW_SIDE_EFFECTS 且给 hint",
                  "EV_ALLOW_SIDE_EFFECTS" in str(result.get("error"))
                  and bool(result.get("hint")),
                  str(result.get("error"))[:200])
    except Exception as exc:  # noqa: BLE001
        fail("EV_ALLOW_SIDE_EFFECTS=0 只读模式验证", f"session/调用异常：{exc}")


async def tier_v2_fake_fallbacks(_workspace: TempWorkspace) -> None:
    """dupe:/rg 降级路径：本用例**现场造数据**，不依赖外层临时目录的生命周期。"""
    print("\n== v2：降级路径（dupe: 不可用 / rg 缺失，内存传输 + monkeypatch） ==")
    import everything_mcp.compose_tools as compose_module
    import everything_mcp.rg as rg_module

    fb = TempWorkspace()
    (fb.root / "a").mkdir(parents=True, exist_ok=True)
    (fb.root / "b").mkdir(parents=True, exist_ok=True)
    (fb.root / "a" / f"{fb.token}_dup.txt").write_bytes(b"fallback-same\n")
    (fb.root / "b" / f"{fb.token}_dup.txt").write_bytes(b"fallback-same\n")

    original_dupe = compose_module._dupe_supported
    original_find = rg_module.find_rg
    try:
        # 现场造的同名文件必须等 Everything 索引可见（python 写入 → 1~5s）
        async with memory_session() as session:
            indexed = await wait_index_total(session, fb.token, 2, timeout=30)
        if indexed != 2:
            skip("dupe: 回退测试数据索引",
                 f"现场造的同名文件 30s 内未被索引（total={indexed}）")
        else:
            compose_module._dupe_supported = lambda client: False
            async with memory_session() as session:
                data = await call_json(
                    session, "everything_duplicates",
                    {"mode": "name", "query": fb.token,
                     "path_scope": fb.scope(), "max_files": 500},
                )
                stats = data.get("stats") or {}
                fallback_warned = any("回退分页扫描" in str(warning)
                                      for warning in data.get("warnings") or [])
                check("dupe: 不可用时 duplicates 回退分页扫描",
                      stats.get("dupe_support") is False and fallback_warned,
                      json.dumps({k: stats.get(k) for k in
                                  ("dupe_support", "dupe_fallback", "dupe_query_used")},
                                 ensure_ascii=False))
                check("回退分页扫描后仍能找到同名重复组",
                      int(stats.get("groups_found") or 0) >= 1,
                      f"groups_found={stats.get('groups_found')}")

        # rg 缺失：path_scope 用始终存在的项目目录，避免依赖临时目录生命周期
        rg_module.find_rg = lambda explicit=None: None
        async with memory_session() as session:
            text = await expect_error(
                session, "everything_grep",
                {"pattern": "EVAL_MARKER", "path_scope": str(REPO)},
                "rg 缺失时 grep 返回可操作错误",
                ("未找到 ripgrep", "EV_RG_EXE"),
            )
            check("rg 缺失错误包含 DSH 自带路径提示",
                  "ripgrep-win32-x64" in text or "winget" in text,
                  text.replace("\n", " ")[:200])
    except Exception as exc:  # noqa: BLE001
        fail("降级路径（dupe/rg）验证", f"monkeypatch/session 异常：{exc}")
    finally:
        compose_module._dupe_supported = original_dupe
        rg_module.find_rg = original_find
        fb.cleanup()
        try:
            async with memory_session() as session:
                await wait_index_total(session, fb.token, 0, timeout=20)
        except Exception:  # noqa: BLE001 - 清理阶段失败不改变测试结论
            pass


async def tier_v2_perf(session: ClientSession, workspace: TempWorkspace) -> None:
    print("\n== v2：性能（search 热 P95 / count / summary / duplicates / grep） ==")
    await call_json(session, "everything_search", {"query": "everything.exe", "max_results": 10})
    timings: list[float] = []
    for _ in range(8):
        started = time.perf_counter()
        await call_json(session, "everything_search",
                        {"query": "everything.exe", "max_results": 10})
        timings.append((time.perf_counter() - started) * 1000)
    ordered = sorted(timings)
    p95 = ordered[max(0, min(len(ordered) - 1, round(0.95 * len(ordered)) - 1))]
    check("v2 搜索热 P95 < 300ms", p95 < 300,
          f"P95={p95:.1f}ms 全部={[round(value, 1) for value in timings]}")
    INFO.append(f"v2 搜索热 P95={p95:.1f}ms（中位 {ordered[len(ordered) // 2]:.1f}ms，"
                f"n={len(timings)}）")

    count_timings: list[float] = []
    for _ in range(5):
        started = time.perf_counter()
        await call_json(session, "everything_count", {"query": "blender"})
        count_timings.append((time.perf_counter() - started) * 1000)
    count_avg = sum(count_timings) / len(count_timings)
    check("v2 count 平均 < 100ms", count_avg < 100,
          f"avg={count_avg:.1f}ms 全部={[round(value, 1) for value in count_timings]}")
    INFO.append(f"v2 count 平均 {count_avg:.1f}ms")

    summary = await call_json(
        session, "everything_summary",
        {"query": workspace.token, "path_scope": workspace.scope(),
         "group_by": "ext", "max_scan": 1000},
    )
    duplicates = await call_json(
        session, "everything_duplicates",
        {"mode": "name", "query": workspace.token, "path_scope": workspace.scope(),
         "max_files": 500},
    )
    grepped = await call_json(
        session, "everything_grep",
        {"pattern": "EVAL_MARKER", "path_scope": workspace.scope(), "max_results": 50},
    )
    check("v2 summary < 5000ms", float(summary.get("elapsed_ms") or 0) < 5000,
          f"elapsed={summary.get('elapsed_ms')}ms")
    check("v2 duplicates(name) < 5000ms", float(duplicates.get("elapsed_ms") or 0) < 5000,
          f"elapsed={duplicates.get('elapsed_ms')}ms")
    check("v2 grep < 5000ms", float(grepped.get("elapsed_ms") or 0) < 5000,
          f"elapsed={grepped.get('elapsed_ms')}ms")
    INFO.append(
        "v2 组合工具耗时：summary={:.1f}ms duplicates(name)={:.1f}ms grep={:.1f}ms".format(
            float(summary.get("elapsed_ms") or 0),
            float(duplicates.get("elapsed_ms") or 0),
            float(grepped.get("elapsed_ms") or 0),
        )
    )


# ----------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------
async def main() -> int:
    global MODE
    print(f"everything-mcp 自测：端口={PORT}，工作区={WORKSPACE}，传输模式={TRANSPORT}")
    print(f"服务器入口：{SERVER_SCRIPT}")

    workspace = TempWorkspace()
    try:
        workspace.setup()
        print(f"[INFO] 临时测试数据：{workspace.root}（python 创建，测试后清理）")
        async with client_session(PORT) as session:
            await tier_a(session)
            await tier_b(session)
            await tier_v2_surface(session)
            await tier_v2_query(session)
            await tier_v2_temp(session, workspace)
            await tier_v2_boundaries(session, workspace)
            await tier_v2_perf(session, workspace)
        print(
            "[INFO] 实际传输模式："
            + {
                "stdio": "官方 SDK stdio 子进程",
                "pipes": "匿名管道 stdio 子进程（MCP 标准 JSON-RPC）",
                "memory": "MCP 内存传输（沙箱降级）",
            }.get(MODE, MODE)
        )

        await tier_error_path()
        await tier_entries()

        # 独立 session 验证只读模式（EV_ALLOW_SIDE_EFFECTS=0）
        await tier_side_effects_zero(workspace)
        # 内存传输 + monkeypatch 验证 dupe:/rg 降级路径
        await tier_v2_fake_fallbacks(workspace)

        # 清理临时数据并验证 Everything 索引回收（1~5s）
        workspace.cleanup()
        async with client_session(PORT) as session:
            remaining = await wait_index_total(session, workspace.token, 0, timeout=25)
        check("临时数据清理后 Everything 索引回收", remaining == 0,
              f"token={workspace.token} 残留 {remaining} 条")
    finally:
        workspace.cleanup()
        # 清理可能残留的导出/下载临时文件（都在临时目录内，rmtree 已覆盖）
        for leftover in WORKSPACE.glob(f"{workspace.dir_name}*"):
            if leftover.is_dir():
                shutil.rmtree(leftover, ignore_errors=True)
            else:
                leftover.unlink(missing_ok=True)

    print("\n== 汇总 ==")
    for line in INFO:
        print(f"[INFO] {line}")
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项，跳过 {len(SKIP)} 项")
    if FAIL:
        print("失败项：")
        for name in FAIL:
            print(f"  - {name}")
        return 1
    print("全部必须通过项已通过。")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
