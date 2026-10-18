"""ripgrep 探测与内容搜索封装（纯标准库，只读，无副作用）。

本模块只做两件事：

1. **探测 rg.exe**：``EV_RG_EXE`` → DSH 自带稳定路径 → ``shutil.which("rg")``
   → 已知候选（VS Code 等）。找不到时给出可操作的中文错误，不崩溃。
2. **封装内容搜索**：``rg --json``，支持固定字符串、大小写、glob/扩展名过滤、
   ``--no-ignore``、``--max-filesize``、整体超时与结果上限。**只读**，不做任何写操作。

设计要点：
- 模块 import 零副作用（不启动子进程、不探测文件系统，探测全部发生在函数调用时）。
- 输出统一为 ``{"matches": [...], "stats": {...}, "warnings": [...]}``：
  ``matches[]`` 字段 ``path/line/column/text``；``stats`` 字段
  ``files_searched/files_matched/rg_exe/elapsed_ms/truncated``。
- 超时或达到 ``max_results`` 时主动结束 rg 子进程，避免无上限输出把结果塞回模型。
- rg 的 stdout 在后台线程读取，主线程按 deadline 检查，Windows 下同样可靠。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from typing import Any

from .config import _dsh_rg_path

logger = logging.getLogger("everything_mcp.rg")

# 其他已知候选（按顺序探测；全部只做文件存在性检查）。
# DSH 自带路径是动态的（由 %APPDATA% 推导，见 config._dsh_rg_path），不放在这里。
KNOWN_CANDIDATES = (
    os.path.expandvars(
        r"%LOCALAPPDATA%\Programs\Microsoft VS Code\resources\app"
        r"\node_modules\@vscode\ripgrep\bin\rg.exe"
    ),
    os.path.expandvars(
        r"%ProgramFiles%\Microsoft VS Code\resources\app"
        r"\node_modules\@vscode\ripgrep\bin\rg.exe"
    ),
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\VSCodium\resources\app\node_modules\@vscode\ripgrep\bin\rg.exe"),
    r"C:\Program Files\Git\usr\bin\rg.exe",
    r"C:\Program Files\ripgrep\rg.exe",
)

# CREATE_NO_WINDOW：避免在 Windows 上弹出控制台窗口
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 单行文本回传给模型的上限（防止 minified 单行文件撑爆 token）
MAX_TEXT_CHARS = 400
# 单文件匹配上限的额外余量（真正全局上限由主循环控制）
PER_FILE_MATCH_LIMIT = 10000


class RgError(RuntimeError):
    """rg 相关错误的基类（消息为可操作中文）。

    ``kind``：``not_found`` / ``bad_argument`` / ``failed`` / ``timeout``。
    """

    def __init__(self, message: str, *, kind: str = "error", details: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.details = details or {}


def _clean_path(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip().strip('"').strip("'")
    return text or None


def _candidate_list(explicit: str | None = None) -> list[tuple[str, str]]:
    """返回 [(路径, 来源)] 候选列表，按优先级排序去重。"""
    items: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(path: str | None, source: str) -> None:
        if not path:
            return
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            return
        seen.add(key)
        items.append((path, source))

    add(_clean_path(explicit), "explicit")
    add(_clean_path(os.environ.get("EV_RG_EXE")), "EV_RG_EXE")
    add(_clean_path(_dsh_rg_path()), "dsh-bundled")
    add(shutil.which("rg"), "PATH")
    for candidate in KNOWN_CANDIDATES:
        add(candidate, "known")
    return items


def find_rg(explicit: str | None = None) -> str | None:
    """按优先级返回可用的 rg.exe 路径；找不到返回 None。"""
    for path, _source in _candidate_list(explicit):
        if os.path.isfile(path):
            return path
    return None


def _missing_message(checked: list[tuple[str, str]]) -> str:
    dsh_hint = _dsh_rg_path() or (
        r"%APPDATA%\npm\node_modules\@deepseek-ai\dsh\node_modules"
        r"\@vscode\ripgrep-win32-x64\bin\rg.exe"
    )
    lines = [
        "未找到 ripgrep（rg.exe），无法执行内容搜索（everything_grep）。",
        "修复方式（任选其一）：",
        "  1) 安装 ripgrep：winget install BurntSushi.ripgrep.MSVC；",
        "  2) 设置环境变量 EV_RG_EXE 指向 rg.exe，然后重启 MCP server；",
        f"  3) 使用 DSH 自带路径：{dsh_hint}",
        "已探测过的位置：",
    ]
    for path, source in checked:
        lines.append(f"  - [{source}] {path}")
    return "\n".join(lines)


def rg_info(explicit: str | None = None) -> dict:
    """返回 rg 探测结果（供 status/自检/测试使用，无副作用）。"""
    checked = _candidate_list(explicit)
    for path, source in checked:
        if os.path.isfile(path):
            return {
                "available": True,
                "path": path,
                "source": source,
                "checked": [p for p, _ in checked],
            }
    return {
        "available": False,
        "path": None,
        "source": None,
        "checked": [p for p, _ in checked],
        "hint": _missing_message(checked),
    }


def _decode_field(field: Any) -> str | None:
    """解析 rg --json 的 {text: ...} / {bytes: base64} 字段。"""
    if not isinstance(field, dict):
        return None
    text = field.get("text")
    if isinstance(text, str):
        return text
    raw = field.get("bytes")
    if isinstance(raw, str):
        try:
            return base64.b64decode(raw).decode("utf-8", errors="replace")
        except (ValueError, OSError):
            return None
    return None


def _build_args(
    exe: str,
    pattern: str,
    roots: list[str],
    *,
    fixed_strings: bool,
    case_sensitive: bool,
    globs: list[str],
    exts: list[str],
    no_ignore: bool,
    max_filesize: str,
) -> list[str]:
    args = [
        exe,
        "--json",
        "--color=never",
        "--no-messages",
        "--max-filesize",
        max_filesize,
        "--max-count",
        str(PER_FILE_MATCH_LIMIT),
    ]
    if fixed_strings:
        args.append("--fixed-strings")
    if not case_sensitive:
        args.append("--ignore-case")
    if no_ignore:
        args.append("--no-ignore")
    for glob in globs:
        args += ["--glob", glob]
    for ext in exts:
        args += ["--glob", f"*.{ext}"]
    args.append("--")
    args.append(pattern)
    args.extend(roots)
    return args


def _normalize_roots(roots: Any) -> list[str]:
    if isinstance(roots, (str, os.PathLike)):
        candidates = [str(roots)]
    else:
        candidates = [str(item) for item in (roots or []) if str(item).strip()]
    normalized: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        text = item.strip().strip('"')
        if not text:
            continue
        absolute = os.path.abspath(text)
        key = os.path.normcase(absolute)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(absolute)
    return normalized


def search(
    pattern: str,
    roots: Any,
    *,
    fixed_strings: bool = False,
    case_sensitive: bool = False,
    globs: list[str] | None = None,
    exts: list[str] | None = None,
    no_ignore: bool = False,
    max_filesize: str = "10M",
    max_results: int = 200,
    timeout_ms: int = 20000,
    rg_exe: str | None = None,
) -> dict:
    """用 rg 做只读内容搜索。

    参数：
        pattern: 搜索模式（regex 或固定字符串，取决于 ``fixed_strings``）。
        roots: 搜索根目录/文件（str 或 list）。
        globs / exts: rg ``--glob`` 过滤；exts 会自动补 ``*.`` 前缀。
        no_ignore: True 时传 ``--no-ignore``（忽略 .gitignore 等）。
        max_filesize: 传给 ``--max-filesize``（如 ``10M``）。
        max_results: 全局匹配条数上限（达到后终止 rg 进程）。
        timeout_ms: 整体超时（毫秒），超时终止 rg 并返回部分结果+warnings。

    返回 ``{"matches": [{path,line,column,text}], "stats": {...}, "warnings": [...]}``。
    """
    if not isinstance(pattern, str) or not pattern:
        raise RgError("pattern 不能为空：请提供要搜索的文本或正则表达式。", kind="bad_argument")

    exe = find_rg(rg_exe)
    if not exe:
        raise RgError(_missing_message(_candidate_list(rg_exe)), kind="not_found")

    root_list = _normalize_roots(roots)
    if not root_list:
        raise RgError(
            "没有可用的搜索根：请提供已存在的 path_scope（例如 D:\\Projects）。",
            kind="bad_argument",
        )
    missing = [root for root in root_list if not os.path.exists(root)]
    if missing:
        raise RgError(
            "搜索根不存在：" + "；".join(missing) + "\n请提供存在的目录/文件作为 path_scope。",
            kind="bad_argument",
        )

    glob_list = [str(item).strip() for item in (globs or []) if str(item).strip()]
    ext_list: list[str] = []
    for item in exts or []:
        text = str(item).strip().lstrip(".").lower()
        if text:
            ext_list.append(text)

    limit = max(1, min(int(max_results), 100000))
    timeout_s = max(0.1, min(float(timeout_ms), 600000.0) / 1000.0)
    args = _build_args(
        exe,
        pattern,
        root_list,
        fixed_strings=bool(fixed_strings),
        case_sensitive=bool(case_sensitive),
        globs=glob_list,
        exts=ext_list,
        no_ignore=bool(no_ignore),
        max_filesize=str(max_filesize),
    )

    env = os.environ.copy()
    # 不让用户的 RIPGREP_CONFIG_PATH 改变本工具的行为，保证结果可复现
    env.pop("RIPGREP_CONFIG_PATH", None)
    env["RIPGREP_CONFIG_PATH"] = ""

    started = time.perf_counter()
    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            creationflags=_CREATE_NO_WINDOW,
            close_fds=True,
        )
    except OSError as exc:
        raise RgError(
            f"启动 ripgrep 失败：{exc}\nrg 路径：{exe}\n"
            "请确认该文件可执行，或通过 EV_RG_EXE 指向可用的 rg.exe。",
            kind="failed",
        ) from exc

    raw_queue: queue.Queue[bytes | None] = queue.Queue()

    def _reader() -> None:
        try:
            stream = proc.stdout
            if stream is not None:
                for raw_line in iter(stream.readline, b""):
                    raw_queue.put(raw_line)
        except (ValueError, OSError):  # 进程被终止、流关闭
            pass
        finally:
            raw_queue.put(None)

    threading.Thread(target=_reader, name="rg-reader", daemon=True).start()

    matches: list[dict] = []
    files_searched = 0          # 兜底：被搜索且产生 begin 事件的文件数
    summary_searches: int | None = None  # rg 自身统计的“实际搜索文件数”（含无匹配文件）
    matched_files: set[str] = set()
    warnings: list[str] = []
    truncated = False
    timed_out = False
    capped_text = 0
    bad_events = 0
    deadline = started + timeout_s

    while True:
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            timed_out = True
            truncated = True
            break
        try:
            raw_line = raw_queue.get(timeout=min(remaining, 0.25))
        except queue.Empty:
            # 进程可能已经退出，但读取线程仍在解析管道缓冲区。必须等到 None
            # 哨兵（读线程 finally 一定写入）再结束，否则会提前 break 丢结果；
            # 真正的兜底是上面的 deadline。
            continue
        if raw_line is None:
            break
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            bad_events += 1
            continue
        event_type = event.get("type")
        data = event.get("data") or {}
        if event_type == "begin":
            files_searched += 1
            continue
        if event_type == "summary":
            stats = data.get("stats") or {}
            raw_searches = stats.get("searches")
            if isinstance(raw_searches, int) and raw_searches >= 0:
                summary_searches = raw_searches
            continue
        if event_type != "match":
            continue
        path = _decode_field(data.get("path"))
        if not path:
            bad_events += 1
            continue
        raw_text = _decode_field(data.get("lines")) or ""
        text = raw_text.rstrip("\r\n")
        if len(text) > MAX_TEXT_CHARS:
            text = text[:MAX_TEXT_CHARS] + "…"
            capped_text += 1
        column = 1
        submatches = data.get("submatches") or []
        if submatches and isinstance(submatches[0], dict):
            start = submatches[0].get("start")
            if isinstance(start, int) and start >= 0:
                encoded = raw_text.encode("utf-8", errors="replace")
                column = len(encoded[:start].decode("utf-8", errors="replace")) + 1
        try:
            line_number = int(data.get("line_number") or 0)
        except (TypeError, ValueError):
            line_number = 0
        matches.append(
            {"path": path, "line": line_number, "column": column, "text": text}
        )
        matched_files.add(path)
        if len(matches) >= limit:
            truncated = True
            break

    if timed_out or (truncated and proc.poll() is None):
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass

    stderr_text = ""
    try:
        if proc.stderr is not None:
            stderr_text = proc.stderr.read().decode("utf-8", errors="replace").strip()
    except (ValueError, OSError):
        pass

    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    returncode = proc.returncode
    if timed_out:
        warnings.append(
            f"ripgrep 超过 timeout_ms={int(timeout_ms)} 被终止，结果可能不完整；"
            "可缩小 path_scope / 增加过滤条件 / 提高 timeout_ms 后重试。"
        )
    if truncated and not timed_out:
        warnings.append(f"匹配数达到 max_results={limit}，已终止 rg 并截断结果。")
    if capped_text:
        warnings.append(f"有 {capped_text} 行文本超过 {MAX_TEXT_CHARS} 字符，已截断显示。")
    if bad_events:
        warnings.append(f"有 {bad_events} 条 rg 输出无法解析，已跳过。")

    if returncode not in (0, 1) and not timed_out and not truncated:
        detail = stderr_text[:800] if stderr_text else "（rg 未输出错误信息）"
        raise RgError(
            f"ripgrep 执行失败（退出码 {returncode}）：{detail}\n"
            f"rg 路径：{exe}\n"
            "请检查 pattern 是否为正则、max_filesize 格式是否为 10M 这类写法；"
            "也可设置 EV_RG_EXE 指向其他 rg.exe。",
            kind="failed",
            details={"returncode": returncode, "stderr": stderr_text},
        )
    if stderr_text and returncode not in (0, 1) and not timed_out:
        warnings.append(f"rg stderr：{stderr_text[:400]}")

    return {
        "matches": matches,
        "stats": {
            "files_searched": summary_searches if summary_searches is not None else files_searched,
            "files_searched_exact": summary_searches is not None,
            "files_matched": len(matched_files),
            "rg_exe": exe,
            "elapsed_ms": elapsed_ms,
            "truncated": truncated,
            "timed_out": timed_out,
            "max_results": limit,
        },
        "warnings": warnings,
    }
