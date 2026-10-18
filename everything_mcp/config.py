"""配置读取：全部配置均可通过环境变量覆盖。

默认值面向 Windows + voidtools Everything 1.4.x 的常见安装：
``C:\\Program Files\\Everything\\Everything.exe``，内置 HTTP Server 绑定
``127.0.0.1:8090``（只绑 loopback，不对局域网暴露）。

支持的环境变量（v1 既有 + v2 新增，见 SPEC §4）：
    EV_HTTP_HOST          默认 127.0.0.1
    EV_HTTP_PORT          默认 8090
    EV_HTTP_TIMEOUT       单次 HTTP 查询超时（秒），默认 2.0
    EV_EVERYTHING_EXE     Everything.exe 路径（自动拉起时使用）
    EV_EVERYTHING_CONFIG  可选：自动拉起时传给 ``-config`` 的 ini 路径
    EV_AUTO_START         默认 1：Everything 未运行时自动拉起
    EV_START_TIMEOUT      自动拉起的等待超时（秒），默认 30
    EV_MAX_RESULTS_CAP    max_results 上限，默认 1000
    EV_WORKSPACE_ROOT     工作区根目录，默认当前工作目录
    EV_EXPORT_ROOT        导出的允许根目录，默认同 EV_WORKSPACE_ROOT
    EV_DOWNLOAD_ROOT      下载落盘根目录，默认同 EV_WORKSPACE_ROOT
    EV_RG_EXE             ripgrep 路径（默认自动探测）
    EV_PRESETS_FILE       自定义预设文件（相对 EV_WORKSPACE_ROOT 或绝对路径）
    EV_ALLOW_SIDE_EFFECTS 默认 1：是否允许 reveal/open 等副作用操作
    EV_SCAN_CAP           聚合扫描上限（行），默认 50000
    EV_HASH_MAX_BYTES     哈希总量上限（字节），默认 268435456
    EV_LOG_LEVEL          日志级别，默认 INFO
"""

from __future__ import annotations

import glob
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# 默认安装位置（官方安装版）
DEFAULT_EVERYTHING_EXE = r"C:\Program Files\Everything\Everything.exe"
EXE_CANDIDATES = (
    DEFAULT_EVERYTHING_EXE,
    r"C:\Program Files (x86)\Everything\Everything.exe",
)
DEFAULT_PORT = 8090
DEFAULT_SCAN_CAP = 50000
DEFAULT_HASH_MAX_BYTES = 268435456


def _env(name: str) -> str | None:
    """读取环境变量，空字符串视为未设置。"""
    raw = os.environ.get(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _resolve_executable(explicit: str | None) -> str:
    """解析 Everything.exe 路径：显式配置优先，其次按候选路径探测。"""
    if explicit:
        return explicit
    for candidate in EXE_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return DEFAULT_EVERYTHING_EXE


# ----------------------------------------------------------------------
# ripgrep 探测（SPEC §1.4：EV_RG_EXE → DSH 自带 → where rg → 已知候选）
# ----------------------------------------------------------------------
def _dsh_rg_path() -> str | None:
    """DSH 自带的 ripgrep 稳定路径（tasks-4 的 grep 工具依赖同一份）。"""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    return str(
        Path(appdata)
        / "npm"
        / "node_modules"
        / "@deepseek-ai"
        / "dsh"
        / "node_modules"
        / "@vscode"
        / "ripgrep-win32-x64"
        / "bin"
        / "rg.exe"
    )


def _known_rg_candidates() -> list[tuple[str, str]]:
    """已知候选路径（VS Code 扩展 / Codex / scoop 等），按存在性由调用方过滤。"""
    candidates: list[tuple[str, str]] = []
    user = os.environ.get("USERPROFILE") or ""
    if user:
        patterns = (
            ".vscode/extensions/*/node_modules/@vscode/ripgrep/bin/rg.exe",
            ".vscode-insiders/extensions/*/node_modules/@vscode/ripgrep/bin/rg.exe",
            ".cursor/extensions/*/node_modules/@vscode/ripgrep/bin/rg.exe",
            "scoop/apps/ripgrep/current/rg.exe",
            ".cargo/bin/rg.exe",
        )
        for pattern in patterns:
            for match in glob.glob(str(Path(user) / pattern))[:3]:
                candidates.append(("known-candidate", match))
    for path in (
        r"C:\ProgramData\chocolatey\bin\rg.exe",
        r"C:\Program Files\ripgrep\rg.exe",
    ):
        candidates.append(("known-candidate", path))
    return candidates


def find_rg(explicit: str = "") -> tuple[str | None, str]:
    """按 SPEC §1.4 的顺序探测 ripgrep。

    返回 ``(path, source)``；找不到时 ``path`` 为 ``None``，``source`` 为
    ``not_found`` 或失败原因（如 ``EV_RG_EXE 指向的文件不存在``）。
    """
    explicit = (explicit or "").strip()
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return str(path), "EV_RG_EXE"
        return None, f"EV_RG_EXE 指向的文件不存在：{explicit}"
    dsh = _dsh_rg_path()
    if dsh and Path(dsh).is_file():
        return dsh, "dsh-bundled"
    which = shutil.which("rg")
    if which:
        return which, "PATH"
    for label, candidate in _known_rg_candidates():
        if Path(candidate).is_file():
            return candidate, label
    return None, "not_found"


@dataclass(frozen=True)
class Config:
    """服务器配置（不可变）。"""

    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    exe: str = DEFAULT_EVERYTHING_EXE
    config_ini: str | None = None
    auto_start: bool = True
    start_timeout: float = 30.0
    http_timeout: float = 2.0
    max_results_cap: int = 1000
    workspace_root: Path = field(default_factory=Path.cwd)
    export_root: Path = field(default_factory=Path.cwd)
    download_root: Path = field(default_factory=Path.cwd)
    rg_exe: str = ""
    presets_file: Path | None = None
    allow_side_effects: bool = True
    scan_cap: int = DEFAULT_SCAN_CAP
    hash_max_bytes: int = DEFAULT_HASH_MAX_BYTES

    @classmethod
    def from_env(cls) -> "Config":
        workspace = Path(_env("EV_WORKSPACE_ROOT") or Path.cwd()).resolve()
        export_root = Path(_env("EV_EXPORT_ROOT") or workspace).resolve()
        download_root = Path(_env("EV_DOWNLOAD_ROOT") or workspace).resolve()
        port = _env_int("EV_HTTP_PORT", DEFAULT_PORT)
        if not 1 <= port <= 65535:
            port = DEFAULT_PORT
        presets_raw = _env("EV_PRESETS_FILE")
        presets_file: Path | None = None
        if presets_raw:
            candidate = Path(presets_raw)
            presets_file = (
                candidate if candidate.is_absolute() else workspace / candidate
            ).resolve()
        return cls(
            host=_env("EV_HTTP_HOST") or "127.0.0.1",
            port=port,
            exe=_resolve_executable(_env("EV_EVERYTHING_EXE")),
            config_ini=_env("EV_EVERYTHING_CONFIG"),
            auto_start=_env_bool("EV_AUTO_START", True),
            start_timeout=max(3.0, _env_float("EV_START_TIMEOUT", 30.0)),
            http_timeout=max(0.2, _env_float("EV_HTTP_TIMEOUT", 2.0)),
            max_results_cap=max(1, _env_int("EV_MAX_RESULTS_CAP", 1000)),
            workspace_root=workspace,
            export_root=export_root,
            download_root=download_root,
            rg_exe=_env("EV_RG_EXE") or "",
            presets_file=presets_file,
            allow_side_effects=_env_bool("EV_ALLOW_SIDE_EFFECTS", True),
            scan_cap=max(100, _env_int("EV_SCAN_CAP", DEFAULT_SCAN_CAP)),
            hash_max_bytes=max(0, _env_int("EV_HASH_MAX_BYTES", DEFAULT_HASH_MAX_BYTES)),
        )

    @property
    def http_url(self) -> str:
        """HTTP Server 根地址。"""
        return f"http://{self.host}:{self.port}/"

    @property
    def rg(self) -> tuple[str | None, str]:
        """当前生效的 ripgrep ``(path, source)``。"""
        return find_rg(self.rg_exe)

    def describe(self) -> dict:
        """用于 status 工具的可序列化配置摘要。"""
        rg_path, rg_source = self.rg
        return {
            "http_url": self.http_url,
            "http_timeout_s": self.http_timeout,
            "everything_exe": self.exe,
            "everything_exe_exists": Path(self.exe).is_file(),
            "everything_config": self.config_ini,
            "auto_start": self.auto_start,
            "start_timeout_s": self.start_timeout,
            "max_results_cap": self.max_results_cap,
            "workspace_root": str(self.workspace_root),
            "export_root": str(self.export_root),
            "download_root": str(self.download_root),
            "rg_exe": self.rg_exe or None,
            "rg_resolved": rg_path,
            "rg_source": rg_source,
            "presets_file": str(self.presets_file) if self.presets_file else None,
            "allow_side_effects": self.allow_side_effects,
            "scan_cap": self.scan_cap,
            "hash_max_bytes": self.hash_max_bytes,
        }
