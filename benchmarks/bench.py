#!/usr/bin/env python
"""Benchmark: native Windows file operations vs. the Everything index via this MCP server.

For every (corpus, scenario) pair the harness measures:

* **native** — the idiomatic PowerShell equivalent (``Get-ChildItem`` /
  ``Where-Object`` / ``Group-Object`` / ``findstr``) executed inside one
  ``-NoProfile`` PowerShell process by ``native_baseline.ps1``;
* **mcp** — the same query through the Python functions the MCP tools wrap
  (``client.count``, ``client.search``, ``compose_tools.*_impl``), measured
  in-process;
* a separate end-to-end MCP stdio round-trip, to quantify the JSON-RPC overhead.

Both sides see the same corpus and the same predicate. Counts are compared and
mismatches are reported (``count_match``) instead of being hidden.

Usage::

    python benchmarks/gen_dataset.py            # one-time, creates benchmarks/.data
    python benchmarks/bench.py                  # all corpora, 3 reps, charts
    python benchmarks/bench.py --corpus system32 --reps 5 --no-charts
    python benchmarks/bench.py --corpus all --out benchmarks/results/latest.json

Outputs (all under ``benchmarks/``):

    results/latest.json    machine-readable results (schema is stable)
    results/summary.csv    flat table for spreadsheets
    charts/*.png           latency, speedup and scaling charts

The harness is read-only against the file system. It never writes outside
``benchmarks/results`` and ``benchmarks/charts``.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
REPO = BENCH_DIR.parent
DATA_DIR = BENCH_DIR / ".data"
RESULTS_DIR = BENCH_DIR / "results"
CHARTS_DIR = BENCH_DIR / "charts"

sys.path.insert(0, str(REPO))

# Aggregation is capped by EV_SCAN_CAP (default 50k). The big corpus has 384k
# files, so raise the cap for the benchmark run; the value is recorded in meta.
os.environ.setdefault("EV_SCAN_CAP", "600000")
os.environ.setdefault("EV_HASH_MAX_BYTES", str(1024 * 1024 * 1024))

from everything_mcp import compose_tools as ct  # noqa: E402
from everything_mcp.client import EverythingClient  # noqa: E402
from everything_mcp.config import Config  # noqa: E402

TOKEN = "benchmark-token"

# Why a count can legitimately differ between the two sides. Only surfaced for
# rows where count_match is false, so nothing is hidden.
COUNT_NOTES: dict[str, str] = {
    "recent_changes": (
        "Everything's dm: filter is day-granular, while the native baseline used "
        "an exact now-7*24h window, so the index side sees up to ~24h more."
    ),
    "content_grep": (
        "findstr and ripgrep classify binary/long-line files differently; the "
        "MCP side searches text files only."
    ),
    "name_regex": (
        "regex + scope/type filters are applied client-side by the MCP server, "
        "which paginates all matches; counts should converge."
    ),
    "big_file_stats": (
        "Everything's size: units are KB-quantized (size:>=100mb means "
        ">=100*1024 KiB in whole KiB), so boundary files can differ."
    ),
    "duplicates": (
        "Both sides group by exact byte length; the index side scans at most "
        "max_files entries (40000 here)."
    ),
}

CORPORA: list[dict] = [
    {
        "id": "system32",
        "root": r"C:\Windows\System32",
        "label": r"C:\Windows\System32",
        "kind": "real-system",
    },
    {
        "id": "programfiles",
        "root": r"C:\Program Files",
        "label": r"C:\Program Files",
        "kind": "real-system",
    },
    {
        "id": "synthetic",
        "root": str(DATA_DIR),
        "label": "synthetic corpus (30k files / 200 dirs)",
        "kind": "synthetic",
    },
]

SCENARIOS: dict[str, str] = {
    "name_glob": "find *.dll by name",
    "name_regex": "regex filename match",
    "multi_filter": "ext + size + mtime filter",
    "cluster_ext": "group by extension (count)",
    "topn_largest": "top-10 largest files",
    "big_file_stats": "files >= 100 MB (count+size)",
    "duplicates": "same-size duplicate groups",
    "recent_changes": "files modified in last 7 days",
    "content_grep": "search a token inside files",
    "small_scope": "search one 200-file directory",
}

CORPUS_SCENARIOS: dict[str, list[str]] = {
    "system32": [
        "name_glob",
        "name_regex",
        "multi_filter",
        "cluster_ext",
        "topn_largest",
        "big_file_stats",
        "recent_changes",
    ],
    "programfiles": [
        "name_glob",
        "multi_filter",
        "cluster_ext",
        "topn_largest",
        "big_file_stats",
    ],
    "synthetic": [
        "name_glob",
        "name_regex",
        "multi_filter",
        "cluster_ext",
        "topn_largest",
        "big_file_stats",
        "duplicates",
        "recent_changes",
        "content_grep",
        "small_scope",
    ],
}

# File counts of each corpus, used only for the scaling chart x-axis.
CORPUS_FILE_HINT = {"system32": 17616, "programfiles": 427637, "synthetic": 30000}

# Per-corpus query parameters so both sides ask the same question on a corpus
# whose file mix differs (real system trees contain .dll/.exe; the synthetic
# corpus contains .log/.dat).
CORPUS_PARAMS: dict[str, dict] = {
    "system32": {
        "name_glob": "*.dll",
        "glob_ext": "dll",
        "name_regex": r"^api-ms-win-.*-l1-.*\.dll$",
        "filter_exts": ".exe,.dll",
        "filter_query": "ext:exe;dll size:>1mb dm:>=2024-01-01",
        "filter_min_bytes": 1048576,
    },
    "programfiles": {
        "name_glob": "*.dll",
        "glob_ext": "dll",
        "name_regex": r"^api-ms-win-.*-l1-.*\.dll$",
        "filter_exts": ".exe,.dll",
        "filter_query": "ext:exe;dll size:>1mb dm:>=2024-01-01",
        "filter_min_bytes": 1048576,
    },
    "synthetic": {
        "name_glob": "*.log",
        "glob_ext": "log",
        "name_regex": r"^item-\d{5}-.*\.log$",
        "filter_exts": ".log,.dat",
        "filter_query": "ext:log;dat size:>100kb dm:>=2024-01-01",
        "filter_min_bytes": 102400,
    },
}


# ----------------------------------------------------------------------
# MCP side: the same code paths the MCP tools call
# ----------------------------------------------------------------------
def mcp_run(client: EverythingClient, scenario: str, root: str, params: dict) -> tuple[int, dict]:
    if scenario == "name_glob":
        result = client.count(f"ext:{params['glob_ext']}", path_scope=root, file_only=True)
        return int(result["total"]), {}

    if scenario == "name_regex":
        # regex + scope/type filters are applied client-side by the library, and
        # count() is capped by EV_MAX_RESULTS_CAP. Paginate instead so the total
        # is exact and comparable with the native full scan.
        total = 0
        for page in client.search_pages(
            params["name_regex"],
            regex=True,
            path_scope=root,
            file_only=True,
            page_size=1000,
            max_items=None,
        ):
            total += len(page)
        return total, {}

    if scenario == "multi_filter":
        result = client.count(
            params["filter_query"], path_scope=root, file_only=True
        )
        return int(result["total"]), {}

    if scenario == "cluster_ext":
        result = ct.summary_impl(
            client, path_scope=root, group_by="ext", top_n=50, max_scan=600000
        )
        return int(result["totals"]["count"]), {
            "groups": int(result["stats"]["groups_found"]),
            "bytes": int(result["totals"]["size"]),
        }

    if scenario == "topn_largest":
        result = client.search(
            "",
            path_scope=root,
            file_only=True,
            sort="size",
            ascending=False,
            max_results=10,
        )
        items = result.get("results") or result.get("items") or []
        return len(items), {}

    if scenario == "big_file_stats":
        result = ct.summary_impl(
            client,
            query="size:>=100mb",
            path_scope=root,
            group_by="ext",
            max_scan=600000,
        )
        return int(result["totals"]["count"]), {"bytes": int(result["totals"]["size"])}

    if scenario == "duplicates":
        result = ct.duplicates_impl(
            client,
            mode="size",
            path_scope=root,
            max_files=40000,
            max_groups=2000,
        )
        return int(result["stats"]["groups_found"]), {
            "files": int(result["stats"]["scanned"])
        }

    if scenario == "recent_changes":
        since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        result = client.count(f"dm:>={since}", path_scope=root, file_only=True)
        return int(result["total"]), {}

    if scenario == "content_grep":
        result = ct.grep_impl(
            client,
            pattern=TOKEN,
            fixed_strings=True,
            path_scope=root,
            max_results=5000,
            no_ignore=True,  # the corpus is not a git-ignored code tree
        )
        return int(result["stats"].get("files_matched") or 0), {
            "searched": int(result["stats"].get("files_searched") or 0)
        }

    if scenario == "small_scope":
        scope = os.path.join(root, "small_scope")
        result = client.count("ext:txt", path_scope=scope, file_only=True)
        return int(result["total"]), {}

    raise ValueError(f"unknown scenario: {scenario}")


def measure(fn, reps: int) -> list[float]:
    fn()  # warm-up, discarded
    samples: list[float] = []
    for _ in range(reps):
        started = time.perf_counter()
        fn()
        samples.append(round((time.perf_counter() - started) * 1000, 3))
    return samples


# ----------------------------------------------------------------------
# MCP stdio round-trip overhead (JSON-RPC framing + process boundary)
# ----------------------------------------------------------------------
async def _stdio_roundtrips(reps: int = 15) -> list[float] | None:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except Exception:  # pragma: no cover - optional dependency
        return None

    env = dict(os.environ)
    env.update({"EV_SKIP_SELF_CHECK": "1", "EV_LOG_LEVEL": "WARNING", "PYTHONPATH": str(REPO)})
    try:
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(REPO / "server.py")],
            cwd=str(REPO),
            env=env,
        )
    except TypeError:  # older SDK without cwd=
        params = StdioServerParameters(
            command=sys.executable, args=[str(REPO / "server.py")], env=env
        )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            await session.call_tool("everything_count", {"query": "ext:py"})
            samples: list[float] = []
            for _ in range(reps):
                started = time.perf_counter()
                await session.call_tool("everything_count", {"query": "ext:py"})
                samples.append(round((time.perf_counter() - started) * 1000, 3))
    return samples


def mcp_stdio_overhead() -> float | None:
    try:
        samples = asyncio.run(_stdio_roundtrips())
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"[bench] stdio probe failed: {exc}", file=sys.stderr)
        return None
    if not samples:
        return None
    return round(statistics.median(samples), 3)


# ----------------------------------------------------------------------
# Native side (PowerShell)
# ----------------------------------------------------------------------
def find_powershell() -> str:
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in (
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    raise SystemExit("PowerShell not found; install PowerShell 7 or use Windows PowerShell 5.1")


def native_shell_version() -> str:
    """Version string of the shell used for the native baseline."""
    try:
        output = subprocess.run(
            [find_powershell(), "-NoProfile", "-Command", "$PSVersionTable.PSVersion.ToString()"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        version = (output.stdout or "").strip()
        return f"PowerShell {version}" if version else "unknown"
    except Exception:
        return "unknown"


def run_native(root: str, scenarios: list[str], reps: int, params: dict) -> dict[str, dict]:
    command = [
        find_powershell(),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(BENCH_DIR / "native_baseline.ps1"),
        "-Root",
        root,
        "-Scenarios",
        ",".join(scenarios),
        "-Reps",
        str(reps),
        "-Token",
        TOKEN,
        "-NameGlob",
        params["name_glob"],
        "-NameRegex",
        params["name_regex"],
        "-FilterExts",
        params["filter_exts"],
        "-FilterMinBytes",
        str(params["filter_min_bytes"]),
    ]
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if completed.returncode != 0:
        raise SystemExit(
            "native baseline failed:\n"
            f"exit={completed.returncode}\nstdout={completed.stdout[-2000:]}\n"
            f"stderr={completed.stderr[-2000:]}"
        )
    stdout = completed.stdout.strip()
    start = stdout.find("{")
    if start < 0:
        raise SystemExit(f"native baseline produced no JSON:\n{stdout[-2000:]}")
    payload = json.loads(stdout[start:])
    return {item["id"]: item for item in payload["scenarios"]}


# ----------------------------------------------------------------------
# Charts
# ----------------------------------------------------------------------
def make_charts(results: dict, out_dir: Path) -> list[Path]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    scenarios = results["scenarios"]

    def rows(corpus: str) -> list[dict]:
        return [row for row in scenarios if row["corpus"] == corpus]

    # ---- chart 1: latency, largest corpus --------------------------------
    big = rows("programfiles") or rows("system32")
    if big:
        labels = [SCENARIOS[row["scenario"]] for row in big]
        native = [max(row["native_ms"], 0.01) for row in big]
        mcp = [max(row["mcp_ms"], 0.01) for row in big]
        y = np.arange(len(labels))
        fig, ax = plt.subplots(figsize=(11, 5.2))
        height = 0.36
        bars_n = ax.barh(y + height / 2, native, height, label="native PowerShell scan", color="#d9534f")
        bars_m = ax.barh(y - height / 2, mcp, height, label="Everything index (MCP)", color="#2e8b57")
        ax.set_xscale("log")
        ax.set_yticks(y, labels)
        ax.invert_yaxis()
        ax.set_xlim(left=0.5, right=max(native) * 4)
        ax.set_xlabel("median latency (ms, log scale)")
        corpus_label = next(c["label"] for c in results["corpora"] if c["id"] == (big[0]["corpus"]))
        ax.set_title(f"Same query, same corpus: {corpus_label}")
        ax.grid(axis="x", which="both", alpha=0.25)
        ax.legend(loc="lower right")
        for bars, values in ((bars_n, native), (bars_m, mcp)):
            for bar, value in zip(bars, values):
                ax.text(value * 1.08, bar.get_y() + bar.get_height() / 2,
                        f"{value:,.1f} ms", va="center", fontsize=8)
        fig.tight_layout()
        path = out_dir / "latency_programfiles.png"
        fig.savefig(path, dpi=150)
        plt.close(fig)
        written.append(path)

    # ---- chart 2: speedup ----------------------------------------------
    if big:
        ordered = sorted(big, key=lambda row: row["speedup"])
        labels = [SCENARIOS[row["scenario"]] for row in ordered]
        speedups = [max(row["speedup"], 1.0) for row in ordered]
        fig, ax = plt.subplots(figsize=(11, 5.0))
        colors = ["#2e8b57" if value >= 10 else "#e0a800" for value in speedups]
        bars = ax.barh(np.arange(len(labels)), speedups, color=colors)
        ax.set_xscale("log")
        ax.set_xlim(right=max(speedups) * 2.0)
        ax.set_yticks(np.arange(len(labels)), labels)
        ax.set_xlabel("speed-up vs. native scan (x, log scale)")
        ax.set_title("Everything index vs. native scan — speed-up (median)")
        ax.grid(axis="x", which="both", alpha=0.25)
        for bar, value in zip(bars, speedups):
            label = f"x{value:,.1f}" if value < 10 else f"x{value:,.0f}"
            ax.text(value * 1.05, bar.get_y() + bar.get_height() / 2,
                    label, va="center", fontsize=9)
        fig.tight_layout()
        path = out_dir / "speedup.png"
        fig.savefig(path, dpi=150)
        plt.close(fig)
        written.append(path)

    # ---- chart 3: scaling ----------------------------------------------
    # Only the two corpora on the same volume (C:), so the x-axis compares
    # like with like; the synthetic corpus lives on a different disk.
    scaling = [
        row
        for row in scenarios
        if row["scenario"] == "name_glob" and row["corpus"] in ("system32", "programfiles")
    ]
    if len(scaling) >= 2:
        scaling.sort(key=lambda row: CORPUS_FILE_HINT.get(row["corpus"], 0))
        sizes = [CORPUS_FILE_HINT.get(row["corpus"], 0) for row in scaling]
        native = [max(row["native_ms"], 0.01) for row in scaling]
        mcp = [max(row["mcp_ms"], 0.01) for row in scaling]
        labels = [f"{row['corpus']}\n{size:,} files" for row, size in zip(scaling, sizes)]
        fig, ax = plt.subplots(figsize=(8.5, 5.2))
        ax.plot(sizes, native, "o-", color="#d9534f", linewidth=2, label="native PowerShell scan")
        ax.plot(sizes, mcp, "s-", color="#2e8b57", linewidth=2, label="Everything index (MCP)")
        for x_value, y_value in zip(sizes, native):
            ax.annotate(f"{y_value:,.0f} ms", (x_value, y_value), textcoords="offset points",
                        xytext=(0, 8), ha="center", fontsize=8, color="#a94442")
        for x_value, y_value in zip(sizes, mcp):
            ax.annotate(f"{y_value:,.1f} ms", (x_value, y_value), textcoords="offset points",
                        xytext=(0, -14), ha="center", fontsize=8, color="#1e5c3a")
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xticks(sizes, labels)
        ax.set_xlabel("corpus size (files, log scale)")
        ax.set_ylabel("latency (ms, log scale)")
        ax.set_title("'find *.dll': native scan grows with corpus, index query does not")
        ax.grid(which="both", alpha=0.25)
        ax.legend()
        fig.tight_layout()
        path = out_dir / "scaling.png"
        fig.savefig(path, dpi=150)
        plt.close(fig)
        written.append(path)

    return written


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", default="all", choices=["all", *[c["id"] for c in CORPORA]])
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--out", default=str(RESULTS_DIR / "latest.json"))
    parser.add_argument("--no-charts", action="store_true")
    parser.add_argument("--skip-stdio", action="store_true")
    args = parser.parse_args(argv)

    selected = [c for c in CORPORA if args.corpus == "all" or c["id"] == args.corpus]
    if any(c["id"] == "synthetic" for c in selected) and not DATA_DIR.is_dir():
        raise SystemExit(
            "synthetic corpus missing; run: python benchmarks/gen_dataset.py --force"
        )

    client = EverythingClient(Config.from_env())
    status = client.status()
    everything_version = str((status.get("everything") or {}).get("version") or "unknown")
    index_total = (status.get("index") or {}).get("total_items")
    index_db_bytes = (status.get("index_db") or {}).get("size_bytes")

    print(f"[bench] Everything {everything_version}, {index_total:,} indexed entries")
    stdio_ms = None if args.skip_stdio else mcp_stdio_overhead()
    if stdio_ms is not None:
        print(f"[bench] MCP stdio round-trip overhead: {stdio_ms:.2f} ms (median)")

    corpora_meta: list[dict] = []
    rows: list[dict] = []
    started_all = time.perf_counter()

    for corpus in selected:
        root = corpus["root"]
        scenario_ids = CORPUS_SCENARIOS[corpus["id"]]
        reps = args.reps
        if corpus["id"] == "programfiles" and reps > 2:
            reps = 2  # each native pass costs ~16 s warm
        params = CORPUS_PARAMS[corpus["id"]]
        print(f"\n[bench] corpus={corpus['id']} root={corpus['label']} reps={reps}")
        try:
            indexed = client.count("", path_scope=root, file_only=False)["total"]
        except Exception:
            indexed = None

        native = run_native(root, scenario_ids, reps, params)
        for scenario in scenario_ids:
            native_row = native.get(scenario)
            if native_row is None:
                print(f"  ! native result missing for {scenario}")
                continue
            mcp_samples = measure(lambda s=scenario: mcp_run(client, s, root, params), reps)
            mcp_ms = round(statistics.median(mcp_samples), 3)
            native_ms = float(native_row["median_ms"])
            native_count = native_row.get("count")
            mcp_count, extra = mcp_run(client, scenario, root, params)
            speedup = round(native_ms / mcp_ms, 1) if mcp_ms > 0 else None
            match = native_count == mcp_count
            row = {
                "scenario": scenario,
                "corpus": corpus["id"],
                "native_ms": round(native_ms, 2),
                "mcp_ms": mcp_ms,
                "speedup": speedup,
                "native_count": native_count,
                "mcp_count": mcp_count,
                "count_match": bool(match),
                "count_note": None if match else COUNT_NOTES.get(scenario),
                "native_samples_ms": native_row.get("samples_ms"),
                "mcp_samples_ms": mcp_samples,
                "extra": {**(native_row.get("extra") or {}), **extra},
            }
            rows.append(row)
            flag = "" if match else f"  <-- count mismatch ({native_count} vs {mcp_count})"
            print(
                f"  {scenario:<15} native {native_ms:>10,.1f} ms   mcp {mcp_ms:>8,.2f} ms"
                f"   x{speedup if speedup else 0:>9,.0f}{flag}"
            )
        corpora_meta.append(
            {
                "id": corpus["id"],
                "label": corpus["label"],
                "kind": corpus["kind"],
                "indexed_entries": indexed,
                "files_native": CORPUS_FILE_HINT.get(corpus["id"]),
            }
        )

    elapsed = round(time.perf_counter() - started_all, 1)
    result = {
        "meta": {
            "timestamp": datetime.now().astimezone().isoformat(timespec="seconds"),
            "os": f"{platform.system()} {platform.release()} ({platform.version()})",
            "cpu": os.environ.get("PROCESSOR_IDENTIFIER") or platform.processor() or "unknown",
            "machine": platform.machine(),
            "ram_gb": round(os_sysconf_ram(), 1),
            "python": platform.python_version(),
            "native_shell": native_shell_version(),
            "everything_version": everything_version,
            "everything_entries": index_total,
            "index_db_bytes": index_db_bytes,
            "reps": args.reps,
            "programfiles_reps": min(args.reps, 2),
            "scan_cap": os.environ.get("EV_SCAN_CAP"),
            "mcp_stdio_overhead_ms": stdio_ms,
            "total_runtime_s": elapsed,
            "notes": (
                "native = warm idiomatic PowerShell scan in one -NoProfile process "
                "(shell startup excluded, one warm-up discarded); mcp = in-process call "
                "to the function behind the MCP tool. Counts are compared; count_match "
                "records discrepancies caused by index scope (hidden/system files, "
                "junctions) or Everything's KB-quantized size: syntax."
            ),
        },
        "corpora": corpora_meta,
        "scenarios": rows,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    csv_path = out_path.with_name("summary.csv")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["corpus", "scenario", "native_ms", "mcp_ms", "speedup",
             "native_count", "mcp_count", "count_match"]
        )
        for row in rows:
            writer.writerow(
                [row["corpus"], row["scenario"], row["native_ms"], row["mcp_ms"],
                 row["speedup"], row["native_count"], row["mcp_count"], row["count_match"]]
            )

    charts: list[Path] = []
    if not args.no_charts:
        try:
            charts = make_charts(result, CHARTS_DIR)
        except Exception as exc:  # pragma: no cover - charting is optional
            print(f"[bench] chart generation failed: {exc}", file=sys.stderr)

    print(f"\n[bench] wrote {out_path}")
    print(f"[bench] wrote {csv_path}")
    for chart in charts:
        print(f"[bench] wrote {chart}")
    print(f"[bench] total runtime: {elapsed:.0f} s")
    mismatches = [row for row in rows if not row["count_match"]]
    if mismatches:
        print(f"[bench] {len(mismatches)} count mismatch(es):")
        for row in mismatches:
            print(f"  - {row['corpus']}/{row['scenario']}: "
                  f"{row['native_count']} native vs {row['mcp_count']} mcp")
    return 0


def os_sysconf_ram() -> float:
    """Total physical RAM in GiB (Windows via ctypes, POSIX via sysconf)."""
    try:
        if os.name == "nt":
            import ctypes

            class MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatusEx()
            status.dwLength = ctypes.sizeof(MemoryStatusEx)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return status.ullTotalPhys / (1024 ** 3)
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3)
    except Exception:
        return 0.0


if __name__ == "__main__":
    sys.exit(main())
