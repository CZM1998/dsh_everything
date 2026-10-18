"""Windows 平台辅助：进程/窗口探测与 exe 版本读取（ctypes，零第三方依赖）。

用途：
- 判断 Everything.exe 是否在运行（自动拉起与错误提示需要区分
  “未运行”和“已运行但 HTTP 未启用”）；
- 读取 Everything.exe 的文件版本，供 ``everything_status`` 展示；
- 枚举 Everything 的 IPC 窗口，作为诊断信息。

注意：本模块**不实现** WM_COPYDATA 查询协议。Everything 1.4 的 IPC 查询
结果需要通过 SDK 的共享内存回复协议获取，本机没有 SDK 且不允许下载；当前
方案用 HTTP Server 作为查询通道（毫秒级），IPC 只用于“是否在运行”的探测。
详见 README 的“查询通道”一节。
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes as wt
from pathlib import Path

IS_WINDOWS = os.name == "nt"

TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260
# Everything 进程可执行文件名（不区分大小写）
EVERYTHING_EXE_NAMES = {"everything.exe"}
# Everything 主窗口类名以该前缀开头（命名实例会在后面追加实例名）
EVERYTHING_WINDOW_CLASS_PREFIX = "EVERYTHING"

if IS_WINDOWS:
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
else:  # pragma: no cover - 非 Windows 仅用于导入期兼容
    INVALID_HANDLE_VALUE = -1


class PROCESSENTRY32W(ctypes.Structure):
    """Toolhelp32 进程快照结构（避免引入 psutil）。"""

    _fields_ = [
        ("dwSize", wt.DWORD),
        ("cntUsage", wt.DWORD),
        ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD),
        ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wt.DWORD),
        ("szExeFile", wt.WCHAR * MAX_PATH),
    ]


_WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def enumerate_processes() -> list[dict]:
    """枚举所有进程，返回 [{"pid": int, "name": str}]（失败时返回空列表）。"""
    if not IS_WINDOWS:
        return []
    kernel32 = ctypes.windll.kernel32
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == INVALID_HANDLE_VALUE:
        return []
    processes: list[dict] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return []
        while True:
            processes.append(
                {"pid": int(entry.th32ProcessID), "name": entry.szExeFile}
            )
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    return processes


def enumerate_windows() -> list[dict]:
    """枚举所有顶层窗口，返回 [{"hwnd","class","title","pid","visible"}]。"""
    if not IS_WINDOWS:
        return []
    user32 = ctypes.windll.user32
    windows: list[dict] = []

    def _callback(hwnd: int, _lparam: int) -> bool:
        length = user32.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title_buf, length + 1)
        class_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buf, 256)
        pid = wt.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        windows.append(
            {
                "hwnd": int(hwnd),
                "class": class_buf.value,
                "title": title_buf.value,
                "pid": int(pid.value),
                "visible": bool(user32.IsWindowVisible(hwnd)),
            }
        )
        return True

    user32.EnumWindows(_WNDENUMPROC(_callback), 0)
    return windows


def get_everything_processes() -> list[dict]:
    """返回正在运行的 Everything.exe 进程列表。"""
    return [
        proc
        for proc in enumerate_processes()
        if proc["name"].lower() in EVERYTHING_EXE_NAMES
    ]


def get_everything_windows() -> list[dict]:
    """返回 Everything 的窗口（含隐藏的 IPC 通知窗口）。"""
    result = []
    for window in enumerate_windows():
        if window["class"].upper().startswith(EVERYTHING_WINDOW_CLASS_PREFIX):
            result.append(window)
    return result


def is_everything_running() -> bool:
    """Everything 是否正在运行（进程存在，或存在 Everything 窗口）。

    注意：标题为 ``Everything (实例名)`` 的窗口说明使用的是命名实例。
    """
    return bool(get_everything_processes() or get_everything_windows())


def describe_instances() -> dict:
    """汇总 Everything 实例信息，供 everything_status 诊断使用。"""
    processes = get_everything_processes()
    windows = get_everything_windows()
    instance_names: list[str] = []
    for window in windows:
        title = window.get("title") or ""
        if title.startswith("Everything (") and title.endswith(")"):
            instance_names.append(title[len("Everything ("):-1])
    return {
        "running": bool(processes or windows),
        "processes": processes,
        "process_count": len(processes),
        "windows": windows,
        "instances": sorted(set(instance_names)),
    }


# ----------------------------------------------------------------------
# exe 文件版本
# ----------------------------------------------------------------------
class VS_FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [
        ("dwSignature", wt.DWORD),
        ("dwStrucVersion", wt.DWORD),
        ("dwFileVersionMS", wt.DWORD),
        ("dwFileVersionLS", wt.DWORD),
        ("dwProductVersionMS", wt.DWORD),
        ("dwProductVersionLS", wt.DWORD),
        ("dwFileFlagsMask", wt.DWORD),
        ("dwFileFlags", wt.DWORD),
        ("dwFileOS", wt.DWORD),
        ("dwFileType", wt.DWORD),
        ("dwFileSubtype", wt.DWORD),
        ("dwFileDateMS", wt.DWORD),
        ("dwFileDateLS", wt.DWORD),
    ]


def get_file_version(path: str | Path) -> str | None:
    """读取 exe 的 FileVersion（如 "1.4.1.1024"），失败返回 None。"""
    if not IS_WINDOWS or not Path(path).is_file():
        return None
    version = ctypes.windll.version
    version.GetFileVersionInfoSizeW.argtypes = [wt.LPCWSTR, ctypes.POINTER(wt.DWORD)]
    version.GetFileVersionInfoSizeW.restype = wt.DWORD
    version.GetFileVersionInfoW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID]
    version.GetFileVersionInfoW.restype = wt.BOOL
    version.VerQueryValueW.argtypes = [
        wt.LPVOID,
        wt.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wt.UINT),
    ]
    version.VerQueryValueW.restype = wt.BOOL

    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        return None
    pointer = ctypes.c_void_p()
    length = wt.UINT()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        return None
    if not pointer:
        return None
    info = ctypes.cast(pointer, ctypes.POINTER(VS_FIXEDFILEINFO)).contents
    if info.dwSignature != 0xFEEF04BD:
        return None
    ms, ls = info.dwFileVersionMS, info.dwFileVersionLS
    return f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
