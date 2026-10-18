# dsh_everything

**English** | [简体中文](README.zh-CN.md)

> **Millisecond file search for AI agents on Windows.** `dsh_everything` exposes
> the local [voidtools Everything](https://www.voidtools.com/) index as an
> [MCP](https://modelcontextprotocol.io/) stdio server, so an agent can find,
> filter and aggregate files across millions of index entries instead of
> recursively scanning disks.

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
![Windows 10 / 11](https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-0078D6)
![License: MIT](https://img.shields.io/badge/license-MIT-green)
![MCP stdio](https://img.shields.io/badge/protocol-MCP%20stdio-8A2BE2)
![Everything 1.4 / 1.5](https://img.shields.io/badge/Everything-1.4%20%2F%201.5-6f42c1)

**13 tools** (7 query tools + 6 composite tools) · **3 MCP resources** ·
**no `es.exe`, no Everything SDK, no third-party Python dependencies** beyond the
official `mcp` SDK · **loopback-only**, **read-only by default**.

**Current version: 2.0.0** — see [CHANGELOG.md](CHANGELOG.md).

- [Usage cookbook](docs/USAGE.md) — task-oriented recipes with exact tool calls
- [Capability matrix](docs/CAPABILITIES.md) — every supported / unsupported Everything query, verified on 1.4.1.1024
- [Examples](examples/) — DSH profile patch, generic MCP config, custom presets
- [Benchmarks](benchmarks/) — reproducible native-vs-index harness, charts and full results

---

## Table of contents

1. [Why this exists](#1-why-this-exists)
2. [Features](#2-features)
3. [Requirements](#3-requirements)
4. [Install](#4-install)
5. [Quick start](#5-quick-start)
6. [Tool reference](#6-tool-reference)
7. [Everything query syntax cheat sheet](#7-everything-query-syntax-cheat-sheet)
8. [Recipes](#8-recipes)
9. [Benchmarks](#9-benchmarks)
10. [Architecture](#10-architecture)
11. [Security and privacy](#11-security-and-privacy)
12. [Limitations](#12-limitations)
13. [Troubleshooting](#13-troubleshooting)
14. [Development and tests](#14-development-and-tests)
15. [License and acknowledgements](#15-license-and-acknowledgements)

- [Changelog](CHANGELOG.md)

---

## 1. Why this exists

An agent that needs a file usually has two bad options:

- **Recursive scan.** `Get-ChildItem -Recurse`, `os.walk`, `find` — every call
  touches every directory, warms the disk cache from scratch, and takes seconds
  to minutes on a real workstation. On a corpus of a few hundred thousand files a
  *warm* PowerShell scan already takes 16–27 s; a cold one is far worse. An agent
  that does this in a
  loop burns wall-clock time and tool-call budget on I/O, not reasoning.
- **Guessing.** Ask the user where the file is, or guess a path and fail.

Everything maintains a persistent NTFS index (built from the USN journal) of file
names, paths, sizes and modification times. A query against that index returns in
single-digit milliseconds for millions of entries, with no disk walk and no cache
warm-up. `dsh_everything` puts that index behind an MCP server so the agent can use
it directly.

Concrete agent tasks this unlocks:

- *"Find the Q3 budget spreadsheet and summarize it"* — `everything_search` on a
  name fragment, newest first, in milliseconds.
- *"What changed in this repo this week?"* — `everything_changes` with `since=7d`.
- *"Why is the disk full?"* — `everything_summary` by extension, then a top-N of
  the largest files.
- *"Are there duplicate photos?"* — `everything_duplicates` in `name_size` or
  `hash` mode.
- *"Where is `TODO` mentioned in the backend?"* — `everything_grep` over the
  candidate tree with ripgrep.
- *"Export all build logs over 10 MB to CSV"* — `everything_export`.

The server does not replace `read`/`edit`/shell tools. It replaces the *search*
step in front of them, which is usually the slow part.

## 2. Features

- **13 tools** designed for agents, not humans:
  - **7 query tools** — `everything_search`, `everything_count`,
    `everything_status`, `everything_help`, `everything_presets`,
    `everything_export`, `everything_download`.
  - **6 composite tools** — `everything_summary`, `everything_duplicates`,
    `everything_grep`, `everything_batch_stat`, `everything_changes`,
    `everything_reveal`.
- **3 MCP resources** — `everything://help/syntax`, `everything://status`,
  `everything://presets`.
- **Everything-native query syntax** — `ext:`, `size:`, `dm:`, `path:`, `parent:`,
  `wfn:`, wildcards, `|` (OR), `!` (NOT), regex, quotes, Chinese file names.
- **Pagination, sorting, field projection and `count_only`** to keep responses
  small enough for a model context window.
- **Aggregation and duplicate detection** with hard scan caps so a query can never
  dump a whole disk into the context window.
- **Content search via ripgrep** — automatic detection (`EV_RG_EXE` → DSH-bundled
  `rg.exe` → `PATH` → common install locations); a missing `rg` produces an
  actionable error, not a crash.
- **Export** to CSV (UTF-8 BOM for Excel), JSONL, or EFU; **download** files through
  the Everything HTTP server. Both are confined to configured root directories.
- **8 built-in presets** (`recent_modified_7d`, `today_changed`,
  `large_files_100mb`, `screenshots_7d`, `archives_100mb`, `git_repos`,
  `node_modules`, `downloads_today`) plus your own via `EV_PRESETS_FILE`.
- **Loopback-only, no telemetry, no network egress.** The only network traffic is
  HTTP to `127.0.0.1:8090` on the same machine.
- **Read-only by default.** Only `everything_export` and `everything_download`
  write files (inside their configured roots); `everything_reveal` is the single
  side-effect tool and can be disabled with `EV_ALLOW_SIDE_EFFECTS=0`.
- **Auto-start** — if Everything is not running, the server can launch it
  minimized (`EV_AUTO_START=1`, the default).
- **Operational diagnostics** — `everything_status` and `everything_help` explain
  the index, the HTTP channel, ini switches, ripgrep and query capabilities instead
  of failing silently.
- **Works with any MCP client.** DSH is the primary target (`mcp__everything__*`
  tool names), but the transport is plain MCP stdio.

## 3. Requirements

| Requirement | Details |
|---|---|
| OS | Windows 10 or 11, on an **NTFS** volume. Everything indexes NTFS; FAT/exFAT and network shares are not indexed unless you explicitly add them. |
| Everything | [voidtools Everything](https://www.voidtools.com/) **1.4.1 or newer**, installed and running. Verified against **1.4.1.1024**; 1.5 is expected to work but the capability matrix has not been re-verified on it. |
| Everything HTTP server | Enabled and bound to **`127.0.0.1:8090`** (see [Install](#4-install)). |
| Python | **3.10+** (tested on 3.11.7), with the official `mcp` SDK **≥ 1.2.0**. |
| MCP client | DSH (DeepSeek Harness) recommended; any MCP stdio client works. |
| ripgrep | Optional, only for `everything_grep`. Auto-detected; see [Troubleshooting](#13-troubleshooting) if it is not found. |
| Privileges | None. No admin rights, no service changes, no files written outside the configured roots. |

## 4. Install

### 4.1 Enable the Everything HTTP server

The server talks to Everything's built-in HTTP JSON API. It is **disabled by
default**, and Everything's HTTP interface has **no authentication**, so it must
listen on loopback only.

1. **Quit Everything completely** (tray icon → *Exit*). Editing the ini while it
   runs does not stick: Everything rewrites its settings on exit.
2. Open `%APPDATA%\Everything\Everything.ini` in a text editor. If the file does
   not exist there, look at `C:\Program Files\Everything\Everything.ini`; an
   `app_data=1` line in it redirects settings to `%APPDATA%`.
3. Set exactly these keys (leave the rest untouched):

   ```ini
   http_server_enabled=1
   http_server_port=8090
   http_server_bindings=127.0.0.1
   http_server_allow_file_download=1
   ```

4. Start Everything again.
5. Verify from a terminal:

   ```powershell
   curl.exe "http://127.0.0.1:8090/?search=test&count=1&json=1"
   ```

   A working server answers with JSON such as
   `{"totalResults":123,"results":[...]}`. An empty index is fine at this point as
   long as you get JSON back.

> **The bindings pitfall.** `http_server_bindings` takes a **bare IP address**
> (`127.0.0.1`). Writing `127.0.0.1:8090` makes Everything **silently not start**
> the HTTP server — no error, no listener. The port is the separate
> `http_server_port` key. Never bind `0.0.0.0` or a LAN address: that exposes the
> full file listing and file downloads to the network without authentication.

`http_server_allow_file_download=1` is only required for `everything_download`.
With it enabled, any local process can read any indexed file over loopback HTTP —
that is the point of the tool, but it is a deliberate local trust decision.

<details>
<summary>Automated alternative: <code>scripts\setup.ps1</code></summary>

The repository ships a read-only environment doctor that checks Everything, the
HTTP endpoint, Python and the MCP SDK, and prints the exact DSH profile snippet
with your machine's real paths:

```powershell
pwsh -File scripts\setup.ps1
```

Pass `-EnableHttpServer` to let it stop Everything, back up the ini, write the
three HTTP keys (`enabled`, `port`, `bindings`) and restart Everything. It does
**not** set `http_server_allow_file_download`; add that key manually if you want
`everything_download`.

</details>

### 4.2 Clone and install

Recommended — editable install. This installs the `mcp` dependency and a
`dsh-everything-mcp` console script:

```powershell
git clone https://github.com/CZM1998/dsh_everything.git
cd dsh_everything
python -m pip install -e .
```

From then on either entry point works:

```powershell
dsh-everything-mcp          # console script (installed)
python -m everything_mcp    # module entry point (installed)
```

**Zero-install alternative** — no packaging, just the one dependency and the
script in the repository:

```powershell
git clone https://github.com/CZM1998/dsh_everything.git
cd dsh_everything
python -m pip install "mcp>=1.2.0"
python server.py            # or: python <PATH-TO-REPO>\server.py from any cwd
```

The server speaks MCP over stdio. Started manually it waits for a client
handshake; that is expected. On startup it performs one read-only self-check
against the HTTP endpoint and logs the result to **stderr** (stdout is reserved for
the MCP protocol). A failed self-check does not kill the process — the first tool
call retries and returns actionable guidance.

### 4.3 Register with DSH

DSH mounts MCP servers through `@deepseek-ai/dsh-mcp-client`. Add an entry to the
target profile's patch file:

```text
%USERPROFILE%\.dsh\profiles\web\cordis.patch.yml
```

The patch file is a **top-level YAML array** of loader entries; merge the item
below into the existing array. Replace the three placeholders (`<PYTHON>`,
`<PATH-TO-REPO>`, `<WORKSPACE>`) — the full commented version lives in
[`examples/cordis.patch.yml`](examples/cordis.patch.yml).

```yaml
- id: mcp-everything
  name: "@deepseek-ai/dsh-mcp-client"
  config:
    serverName: everything                 # tools appear as mcp__everything__*
    transport: stdio
    command: <PYTHON>                      # e.g. C:\Python311\python.exe
    args:
      - <PATH-TO-REPO>\server.py           # e.g. D:\work\dsh_everything\server.py
    cwd: <WORKSPACE>                       # e.g. D:\work
    env:
      EV_HTTP_HOST: "127.0.0.1"
      EV_HTTP_PORT: "8090"                 # must match http_server_port
      EV_AUTO_START: "1"
      EV_WORKSPACE_ROOT: "<WORKSPACE>"
      EV_EXPORT_ROOT: "<WORKSPACE>"
      EV_DOWNLOAD_ROOT: "<WORKSPACE>"
      PYTHONUTF8: "1"
      PYTHONIOENCODING: "utf-8"
    toolCallTimeoutMs: 30000
    failOnStartupError: false
```

Notes:

- If you did the editable install, you can use `command: dsh-everything-mcp` and
  omit `args` instead of pointing at `server.py`.
- `serverName: everything` is what produces the `mcp__everything__<tool>` prefix.
- `EV_WORKSPACE_ROOT` is the agent's working directory and the default parent for
  the export and download roots. Keep all three consistent unless you deliberately
  want exports somewhere else.
- Keep `failOnStartupError: false` so a broken MCP entry cannot prevent the
  harness from starting.
- DSH strips `DSH_*` and secret-looking environment variables from child processes;
  `EV_*` variables pass through untouched.
- Other MCP clients: use [`examples/mcp.json`](examples/mcp.json) instead.

### 4.4 Restart `dsh web`

Profile changes are applied at startup. Restart `dsh web` after editing the patch.

> **Known issue: live profile edits.** If a session is already running when the
> profile is edited, the running harness can hold stale fibers and every tool call
> then fails with
> `cannot get required service "sessionProjections" in inactive context`.
> Restarting `dsh web` clears it. Editing the profile and restarting is the
> supported path.

### 4.5 Verify

Ask the agent to call `mcp__everything__everything_status`, then
`mcp__everything__everything_search`:

```json
{"query": "ext:py", "max_results": 3}
```

`everything_status` returns `ok: true`, the Everything version, the index entry
count and the HTTP endpoint state. If it reports a problem, follow its `hints`
array or the [Troubleshooting](#13-troubleshooting) table.

### 4.6 Environment variables

All configuration is through environment variables. Every one has a working
default; you only need the ones you want to change.

| Variable | Default | Description |
|---|---|---|
| `EV_HTTP_HOST` | `127.0.0.1` | Everything HTTP server host. Keep loopback. |
| `EV_HTTP_PORT` | `8090` | Port; must match `http_server_port` in `Everything.ini`. |
| `EV_HTTP_TIMEOUT` | `2.0` | Per-query HTTP timeout in seconds. Raise (e.g. `5`) for big pages or downloads. |
| `EV_EVERYTHING_EXE` | auto-detect | `Everything.exe` path used for auto-start. |
| `EV_EVERYTHING_CONFIG` | empty | Advanced: pass `-config <ini>` to the auto-started instance. Usually leave unset. |
| `EV_AUTO_START` | `1` | Launch Everything minimized when the HTTP endpoint is unreachable and no process is running. |
| `EV_START_TIMEOUT` | `30` | Seconds to wait for auto-started Everything. |
| `EV_MAX_RESULTS_CAP` | `1000` | Hard cap for `everything_search.max_results` (a product limit, not an Everything limit). |
| `EV_WORKSPACE_ROOT` | process cwd | Workspace root; base for relative paths. |
| `EV_EXPORT_ROOT` | same as workspace | Only directory tree `everything_export` may write into. |
| `EV_DOWNLOAD_ROOT` | same as workspace | Only directory tree `everything_download` may write into. |
| `EV_RG_EXE` | auto-detect | ripgrep executable for `everything_grep`. |
| `EV_PRESETS_FILE` | empty | Custom presets file (JSON or YAML; relative to workspace or absolute). |
| `EV_ALLOW_SIDE_EFFECTS` | `1` | `0` disables `everything_reveal` (read-only mode); re-read on every call. |
| `EV_SCAN_CAP` | `50000` | Row cap for `everything_summary` / `everything_duplicates` scans. |
| `EV_HASH_MAX_BYTES` | `268435456` | Total hashing budget (256 MiB) for `duplicates(mode=hash)` / `batch_stat`. |
| `EV_LOG_LEVEL` | `INFO` | Log level; logs go to stderr only. |
| `EV_SKIP_SELF_CHECK` | empty | Set to `1` to skip the startup HTTP self-check. |

## 5. Quick start

Five calls that cover most agent needs. "Intent" is what the user says; the JSON
is the exact tool argument object.

### 5.1 Find a file

*"Find the Q3 budget spreadsheet, newest first."*

```json
{
  "query": "ext:xlsx \"Q3 budget\"",
  "sort": "date_modified",
  "ascending": false,
  "max_results": 20,
  "fields": ["name", "path", "size", "date_modified"]
}
```

Returns the newest matching workbooks with full paths. Quotes force an exact
phrase; without them the two words are an AND search.

### 5.2 Count by extension

*"How many Python files are in this project?"*

```json
{"query": "ext:py", "path_scope": "D:\\work\\my-app"}
```

Returns `{"total": 1284, ...}` and no file list — the cheapest existence check.

### 5.3 Search file contents

*"Where is TODO mentioned in the backend?"*

```json
{
  "pattern": "TODO",
  "path_scope": "D:\\work\\my-app",
  "exts": ["py", "ts"],
  "max_results": 50
}
```

Returns `matches[]` with `path`, `line`, `column` and `text`, plus ripgrep stats.
This is ripgrep over the candidate tree, not an Everything index lookup.

### 5.4 Top-N largest files

*"What are the ten biggest ISO images on this machine?"*

```json
{
  "query": "size:>100mb ext:iso",
  "sort": "size",
  "ascending": false,
  "max_results": 10,
  "fields": ["name", "path", "size"]
}
```

`sort=size` with `ascending=false` puts the largest first. Use raw byte values
(no unit suffix) if you need exact thresholds — see §7.

### 5.5 Recent changes

*"What changed in my project this week?"*

```json
{"since": "7d", "path_scope": "D:\\work\\my-app", "max_results": 20}
```

Returns files modified in the window, newest first, with a `window` object that
documents the exact time bounds. See §6.12 for the date-granularity caveat.

## 6. Tool reference

All tools return JSON objects. Errors are raised as MCP tool errors with a
human-readable, actionable message. In DSH, prefix every name with
`mcp__everything__` (e.g. `mcp__everything__everything_search`).

| # | Tool | Kind | What it does | Key parameters | Returns |
|---|---|---|---|---|---|
| 1 | `everything_search` | query | Search files/directories in the Everything index with pagination, sorting and field projection | `query`, `regex`, `path_scope`, `folder_only`, `file_only`, `sort`, `ascending`, `offset`, `max_results`, `fields`, `count_only` | `total`, `returned`, `truncated`, `next_offset`, `results[]`, `warnings[]` |
| 2 | `everything_count` | query | Return only the hit count (cheapest existence check) | `query`, `regex`, `path_scope`, `folder_only`, `file_only` | `total`, `elapsed_ms` |
| 3 | `everything_status` | query | Diagnose Everything, HTTP, index, ini switches, ripgrep, presets, capabilities | — | `ok`, `http`, `everything`, `index`, `index_db`, `index_switches`, `rg`, `presets`, `capabilities`, `hints[]` |
| 4 | `everything_help` | query | Self-explaining capability matrix, tool list, examples and tips | — | structured help + `markdown` string |
| 5 | `everything_presets` | query | List or run built-in / custom preset queries | `action` (`list`\|`run`), `name`, `max_results`, `offset`, `path_scope` | `presets[]` or a full search result |
| 6 | `everything_export` | query | Export hits to CSV / JSONL / EFU inside `EV_EXPORT_ROOT` | `query`, `out_csv`/`out_path`, `format`, `columns`, `offset`, `sort`, `max_results` | `ok`, `path`, `rows`, `bytes`, `total`, `truncated` |
| 7 | `everything_download` | query | Download an indexed file over loopback HTTP into `EV_DOWNLOAD_ROOT` | `path`, `dest`, `max_bytes` | `ok`, `source`, `saved_path`, `bytes`, `max_bytes` |
| 8 | `everything_summary` | composite | Group hits by extension / directory / type / size bucket / month / parent | `query`, `path_scope`, `group_by`, `dir_depth`, `top_n`, `max_scan`, `include_folders` | `groups[]` with count/size stats, `totals`, `stats` |
| 9 | `everything_duplicates` | composite | Find duplicate files by name, name+size, size, or confirmed hash | `mode`, `query`, `path_scope`, `min_size`, `max_groups`, `max_files`, `hash_algo`, `max_hash_bytes`, `max_paths_per_group`, `max_response_bytes` | `groups[]`, `stats`, `warnings[]` |
| 10 | `everything_grep` | composite | ripgrep content search over a path or over Everything candidates | `pattern`, `fixed_strings`, `case_sensitive`, `query`, `path_scope`, `exts`, `globs`, `no_ignore`, `max_results`, `max_filesize`, `timeout_ms` | `matches[]`, `stats`, `truncated` |
| 11 | `everything_batch_stat` | composite | Stat up to 200 paths in one call, optionally hash them | `paths`, `include_hash`, `hash_algo`, `max_hash_bytes` | `items[]`, `exists_count`, `missing_count` |
| 12 | `everything_changes` | composite | List files changed in a time window | `since`, `path_scope`, `exts`, `max_results`, `sort`, `ascending` | `items[]`, `window`, `count`, `total`, `warnings[]` |
| 13 | `everything_reveal` | composite | Show/open a path in File Explorer (only side-effect tool) | `path` | `ok`, `opened`, `mode` |

### 6.1 `everything_search`

Everything-native search with pagination and projection. `regex=true` switches the
query to a regular expression; `path_scope` is composed into the Everything query
as `path:"..."` **and** re-checked client-side (with `regex=true` it degrades to a
client-side prefix filter and reports `approx`/`warnings`). `fields` is a subset of
`type/name/path/size/date_modified/extension`; unrequested columns are not even
fetched from Everything.

```json
{
  "query": "ext:log size:>10mb",
  "sort": "size",
  "ascending": false,
  "max_results": 2,
  "fields": ["name", "path", "size", "date_modified"]
}
```

```json
{
  "query": "ext:log size:>10mb",
  "effective_query": "ext:log size:>10mb",
  "total": 10,
  "returned": 2,
  "truncated": true,
  "next_offset": 2,
  "offset": 0,
  "max_results": 2,
  "sort": "size",
  "ascending": false,
  "results": [
    {"type": "file", "name": "build.log", "path": "D:\\work\\builds\\build.log",
     "size": 53485308, "date_modified": "2025-07-05T06:18:42Z"},
    {"type": "file", "name": "debug.log", "path": "D:\\work\\tools\\debug.log",
     "size": 35820691, "date_modified": "2025-08-30T00:36:35Z"}
  ],
  "warnings": [],
  "elapsed_ms": 4.8,
  "channel": "http"
}
```

Pagination: pass the returned `next_offset` as `offset`; stop when
`truncated=false`. `count_only=true` returns only `total` (same as
`everything_count`). `sort` accepts only `name`, `path`, `size`, `date_modified`;
anything else produces a warning and falls back to name ascending. Name order is
Everything's **natural sort**, not code-point order — do not assert strict
lexicographic order.

### 6.2 `everything_count`

The cheapest question: does it exist, and how many are there?

```json
{"query": "ext:py", "path_scope": "D:\\work\\my-app"}
```

```json
{"query": "ext:py", "effective_query": "ext:py path:\"D:\\work\\my-app\"",
 "total": 1284, "elapsed_ms": 11.4, "channel": "http", "warnings": []}
```

### 6.3 `everything_status`

No arguments. Use it first when anything looks wrong.

```json
{}
```

Trimmed response (paths shown with placeholders):

```json
{
  "ok": true,
  "channel": "http",
  "http": {"url": "http://127.0.0.1:8090/", "reachable": true, "error": null},
  "everything": {"running": true, "version": "1.4.1.1024",
                 "exe": "C:\\Program Files\\Everything\\Everything.exe"},
  "index": {"total_items": 3200000, "ready": true},
  "index_db": {"path": "C:\\Users\\<USER>\\AppData\\Local\\Everything\\Everything.db",
               "size_mb": 128.8},
  "index_switches": {"index_size": "1", "index_date_modified": "1",
                     "index_date_created": "0", "index_date_accessed": "0",
                     "index_attributes": "0", "index_folder_size": "0"},
  "rg": {"available": true, "source": "dsh-bundled", "version": "ripgrep 15.0.0"},
  "presets": {"count": 8},
  "capabilities": {"columns": ["path", "size", "date_modified"],
                   "sort": ["name", "path", "size", "date_modified"]},
  "auto_start": true,
  "hints": [],
  "elapsed_ms": 45.2
}
```

`hints[]` contains actionable advice (for example, which ini switches are disabled
and why `dc:`/`da:`/`attrib:` queries are slow).

### 6.4 `everything_help`

No arguments. The self-explaining entry point: supported syntax, unsupported
syntax, slow queries, HTTP parameter matrix, all 13 tools, example calls and tips.
The same content is available as a markdown string in the `markdown` field and as
the `everything://help/syntax` resource. Call it before guessing whether a query
prefix works.

### 6.5 `everything_presets`

```json
{"action": "list"}
```

```json
{
  "action": "list",
  "count": 8,
  "presets": [
    {"name": "recent_modified_7d", "description": "Files/directories modified in the last 7 days",
     "query": "dm:last7days", "sort": "date_modified", "ascending": false, "max_results": 100},
    {"name": "large_files_100mb", "query": "size:>100mb", "sort": "size", "ascending": false}
  ],
  "warnings": []
}
```

Run one, optionally overriding paging and scope:

```json
{"action": "run", "name": "screenshots_7d", "max_results": 50}
```

Built-ins: `recent_modified_7d`, `today_changed`, `large_files_100mb`,
`screenshots_7d`, `archives_100mb`, `git_repos`, `node_modules`,
`downloads_today`. Add your own with `EV_PRESETS_FILE` (see
[§8.8](#88-custom-presets)).

### 6.6 `everything_export`

Writes a file inside `EV_EXPORT_ROOT` (default: workspace) and returns its path.
CSV is UTF-8 with a BOM so Excel opens it correctly; JSONL is one object per line;
EFU is an Everything file list (`Filename,Size,Date Modified`, FILETIME dates).
Default 5000 rows, hard cap 10000; use `offset` to export in slices.

```json
{
  "query": "ext:log size:>10mb",
  "out_path": "reports/big_logs.csv",
  "format": "csv",
  "max_results": 5000,
  "sort": "size",
  "ascending": false
}
```

```json
{"ok": true, "format": "csv", "path": "D:\\work\\reports\\big_logs.csv",
 "rows": 137, "bytes": 24512, "total": 137, "offset": 0,
 "truncated": false, "elapsed_ms": 19.3, "channel": "http", "warnings": []}
```

Relative output paths resolve against `EV_EXPORT_ROOT`; absolute paths outside it
are rejected.

### 6.7 `everything_download`

Copies an indexed file to `EV_DOWNLOAD_ROOT` (default: workspace) using the
Everything HTTP server. `dest` is optional; a name collision gets a numeric
suffix. `max_bytes` defaults to 2 GiB and the partial file is removed if it is
exceeded.

```json
{"path": "D:\\work\\builds\\release.zip"}
```

```json
{"ok": true, "source": "D:\\work\\builds\\release.zip",
 "saved_path": "D:\\work\\release.zip", "bytes": 33554432,
 "size": 33554432, "max_bytes": 2147483648, "elapsed_ms": 128.7}
```

Requires `http_server_allow_file_download=1`. A 404 usually means the file moved
or the index has not caught up; search again and retry.

### 6.8 `everything_summary`

Groups matches and aggregates count and bytes. `group_by` is one of `ext`, `dir`,
`type`, `size_bucket`, `month`, `parent`. By default only files are aggregated
(directory sizes are 0 and would skew results); `include_folders=true` adds them
with `aggregates="files+folders"`.

```json
{
  "query": "",
  "path_scope": "D:\\work\\my-app",
  "group_by": "ext",
  "top_n": 6,
  "max_scan": 2000
}
```

```json
{
  "query": "", "path_scope": "D:\\work\\my-app", "group_by": "ext",
  "total": 33, "scanned": 33, "truncated_scan": false,
  "groups": [
    {"key": "py", "count": 14, "size": 412707, "min_size": 6159, "max_size": 80833, "avg_size": 29479},
    {"key": "md", "count": 4, "size": 86907, "min_size": 15990, "max_size": 28990, "avg_size": 21727}
  ],
  "totals": {"count": 33, "size": 891287, "min": 777, "max": 92218},
  "stats": {"groups_found": 2, "returned_groups": 2, "elapsed_ms": 92.8},
  "warnings": [], "elapsed_ms": 92.8
}
```

Scans are capped by `max_scan` and `EV_SCAN_CAP`; `truncated_scan=true` means the
numbers are a lower bound. With `group_by=dir`, `dir_depth` (1–12) controls the
directory key depth; with a `path_scope` the keys are relative to it.

### 6.9 `everything_duplicates`

Read-only duplicate detection. Modes:

- `name` — same file name (case-insensitive); uses Everything's `dupe:` when
  available, otherwise falls back to a paged scan with a warning.
- `name_size` — same name and same size (the safest quick duplicate signal).
- `size` — files sharing a size (≥ `min_size`).
- `hash` — hashes same-size candidates (`md5`/`sha1`/`sha256`/`sha512`/`blake2b`)
  to confirm real duplicates, within `max_files` and `max_hash_bytes` budgets.

```json
{"mode": "name_size", "path_scope": "D:\\work\\photos", "min_size": 1048576, "max_groups": 5}
```

```json
{
  "mode": "name_size", "query_used": "dupe:", "total": 4, "scanned": 4,
  "groups": [
    {"key": "photo.jpg|2048", "count": 2, "size": 2048, "path_count": 2,
     "paths": ["D:\\work\\photos\\backup\\photo.jpg", "D:\\work\\photos\\photo.jpg"]}
  ],
  "stats": {"groups_found": 1, "returned_groups": 1, "dupe_support": true,
            "max_paths_per_group": 2, "payload_bytes": 1147},
  "warnings": [], "elapsed_ms": 940.1
}
```

Output is budgeted for a model context: 10 groups by default (max 50), only the
first 2 paths per group (max 50, full count in `path_count`), and a 4 KiB response
budget (`max_response_bytes`, `0` disables). It never deletes or modifies files.
`dupe:` scans the whole index and takes ~200 ms — far slower than a normal query,
so avoid calling it in tight loops.

### 6.10 `everything_grep`

Content search with ripgrep. Give either `path_scope` or a `query`; with a query,
the server derives up to 8 search roots from the common parent directories of the
candidate files.

```json
{
  "pattern": "def main",
  "path_scope": "D:\\work\\my-app\\backend",
  "exts": ["py"],
  "max_results": 3
}
```

```json
{
  "pattern": "def main", "path_scope": "D:\\work\\my-app\\backend",
  "roots": ["D:\\work\\my-app\\backend"],
  "matches": [{"path": "D:\\work\\my-app\\backend\\server.py", "line": 468,
               "column": 1, "text": "def main() -> None:"}],
  "stats": {"files_searched": 11, "files_matched": 1, "elapsed_ms": 118.7,
            "truncated": false, "timed_out": false, "max_results": 3},
  "truncated": false, "warnings": [], "elapsed_ms": 184.7
}
```

`max_results` ≤ 1000, `max_filesize` (e.g. `10M`), `timeout_ms` default 20000
(100–300000), `no_ignore=true` to ignore `.gitignore`. A missing ripgrep returns
an error listing the paths it probed and how to fix it (`winget install
BurntSushi.ripgrep.MSVC` or set `EV_RG_EXE`); other tools keep working.

### 6.11 `everything_batch_stat`

Stats up to 200 paths in one call (1–200; extras are truncated with a warning,
duplicates de-duplicated). Works for files, directories and missing paths; optional
hashing is read-only and budgeted.

```json
{"paths": ["D:\\work\\my-app\\README.md", "D:\\work\\missing.txt"], "include_hash": true}
```

```json
{
  "items": [
    {"path": "D:\\work\\my-app\\README.md", "exists": true, "type": "file",
     "size": 28990, "date_modified": "2026-10-18T03:05:04Z",
     "date_created": "2026-10-18T02:34:20Z", "mode": "0666", "is_link": false,
     "hash": "9f2c...", "error": null},
    {"path": "D:\\work\\missing.txt", "exists": false, "type": null, "size": null,
     "error": "not found"}
  ],
  "count": 2, "exists_count": 1, "missing_count": 1,
  "include_hash": true, "hash_algo": "md5", "stats": {"elapsed_ms": 1.1, "hashed": 1}
}
```

### 6.12 `everything_changes`

Files changed inside a window. `since` accepts `today`, `24h`, `7d`, `30m`,
`last7days`, or an ISO timestamp (e.g. `2026-10-17T08:00:00+08:00`).

```json
{"since": "24h", "path_scope": "D:\\work\\my-app", "exts": ["py", "ts"], "max_results": 3}
```

```json
{
  "since": "24h", "query": "dm:>=2026-10-17", "path_scope": "D:\\work\\my-app",
  "items": [
    {"type": "file", "name": "server.py", "path": "D:\\work\\my-app\\backend\\server.py",
     "size": 18220, "date_modified": "2026-10-18T03:15:07Z", "extension": "py"}
  ],
  "count": 1, "total": 33, "truncated": true,
  "window": {"since": "2026-10-17T03:27:59Z", "until": "2026-10-18T03:27:59Z",
             "spec": "24h", "dm_expression": "dm:>=2026-10-17", "precise": true},
  "warnings": ["<explains that the dm: filter is date-level and the exact window is applied client-side>"]
}
```

Why the warning: Everything 1.4 does not understand `dm:last1hour`-style relative
syntax (it returns 0). The server always converts `since` into an absolute
`dm:>=YYYY-MM-DD` filter and then applies the exact time window client-side.
`total` is the date-level candidate count; `count` is the exact in-window count.

### 6.13 `everything_reveal`

The only side-effect tool: opens File Explorer with the file selected, or opens a
directory. It never deletes, moves, copies or renames anything. With
`EV_ALLOW_SIDE_EFFECTS=0` it starts no process and returns
`{"ok": false, "opened": false, "error": ..., "hint": ...}`; the variable is
re-read on every call, so no restart is needed to toggle it.

```json
{"path": "D:\\work\\my-app\\README.md"}
```

```json
{"ok": true, "opened": true, "mode": "select",
 "path": "D:\\work\\my-app\\README.md", "warnings": []}
```

### 6.14 MCP resources

| URI | MIME type | Content |
|---|---|---|
| `everything://help/syntax` | `text/markdown` | Capability matrix markdown (same content as `everything_help.markdown`) |
| `everything://status` | `application/json` | Current status JSON (same as `everything_status`) |
| `everything://presets` | `application/json` | Preset catalogue JSON (built-in + `EV_PRESETS_FILE`) |

Resource read failures return an explanatory payload instead of breaking tool
calls; the tools remain available.

## 7. Everything query syntax cheat sheet

The `query` string is passed to Everything almost verbatim, so Everything's own
syntax applies. This is the verified 1.4.1 subset that matters most; the full
matrix with measured hit counts is in
[`docs/CAPABILITIES.md`](docs/CAPABILITIES.md).

### 7.1 Names, paths and filters

```text
report_2025*              wildcard in the file name
"Q3 budget"               exact phrase (quotes)
ext:xlsx                  one extension
ext:py;ts;js              several extensions — SEMICOLONS only
file:report               files only
folder:node_modules       directories only
path:"D:\work\my-app"     path contains (includes subdirectories)
parent:"D:\work\my-app"   direct children of a path — must be a FULL path
wfn:*.exe                 match the whole file name
startwith:report          name prefix
endwith:.log              name suffix
len:>40                   name length
empty:                    empty directories
nopath:foo                ignore path matches
```

`ext:` accepts only semicolons: `ext:exe,dll` and `ext:py*` silently match nothing.
`parent:` requires the complete path; `parent:software` returns nothing.
`path:` is a *contains* match over the whole path, so it also matches subdirectories.
Path filtering only works **inside the search string**: the HTTP API's `path=`
parameter is a boolean match-path switch, not a filter, and `filter=` is ignored.
The `path_scope` tool argument composes `path:"..."` into the query for you.

### 7.2 Boolean logic and quoting

```text
blender ext:exe           space = AND
jpg | png                 | = OR
blender !everything.exe   ! = NOT
```

There is **no word `OR`** — write `jpg | png`. Quotes are the only way to search
for a phrase containing spaces.

### 7.3 Size — the KB-quantization trap

`size:` with a unit is **not byte-exact**. Everything converts `kb`/`mb`/`gb` to a
KB count `U` and compares `ceil(size / 1024)` against it. In practice:

| Query | What it really matches |
|---|---|
| `size:1024` / `size:1024b` | exactly 1024 bytes (byte-exact) |
| `size:1kb` | 1–1024 bytes |
| `size:<1kb` | only 0-byte files |
| `size:2kb` | 1025–2048 bytes |
| `size:1mb` | 1 047 553–1 048 576 bytes — **not** exactly 1 MiB |
| `size:1k` / `size:1m` (no `b`) | the unit is ignored, equals `size:1` / `size:1048576` bytes |
| `size:1024..2048` | range, endpoints inclusive |
| `size:>100mb` | more than 100 MB (still KB-quantized) |

For exact byte thresholds use a unitless value or a range:
`size:>104857600`, `size:1047553..1048576`.

### 7.4 Dates

```text
dm:today
dm:last7days
dm:>=2026-10-17
dm:2026-10-17..2026-10-18
dm:2026-10
```

`dm:` is modification time and is indexed by default. Hour-level relative forms
(`dm:last1hour`, …) return **0** on 1.4 — use `everything_changes` or an absolute
range. `dc:` (created), `da:` (accessed) and `attrib:` work syntactically but are
not indexed by default and can take **more than 5 seconds**; enable the matching
index switches and rebuild the index first, or just don't use them.

### 7.5 Everything 1.5-only prefixes

These return nothing on 1.4 and are not recommended anywhere in this project:

```text
distinct:    sort:    run_count:    date_run:
```

An unknown `XYZ:` prefix is treated as literal text, not as an error, so a query
using an unsupported function quietly returns 0. If a query returns 0 and the
syntax "should" work, check the prefix in `everything_help`.

### 7.6 Other rules worth knowing

- `sort` is an HTTP parameter, not query syntax; only `name`, `path`, `size`,
  `date_modified` are effective. Unknown values silently fall back to name.
- Name sorting is Everything's **natural sort** (localized, numbers as numbers),
  not Unicode code-point order.
- Case-insensitive by default; wrap the query in `case:` or set `case_sensitive`.
- Chinese paths and file names work as UTF-8 over the HTTP channel.
- Prefer Everything-native syntax to regex: it is faster and clearer. Do not mix
  `regex=true` with `ext:`/`path:` functions.

## 8. Recipes

Full task-oriented versions of these (with agent-prompt examples and expected
output shapes) are in [`docs/USAGE.md`](docs/USAGE.md).

### 8.1 Search then filter

*"Find large installers modified this year under Downloads."*

```json
{
  "query": "ext:msi;exe dm:>=2026-01-01 size:>100mb",
  "path_scope": "D:\\work\\Downloads",
  "sort": "size",
  "ascending": false,
  "max_results": 20,
  "fields": ["name", "path", "size", "date_modified"]
}
```

### 8.2 Content grep with ripgrep

*"Find every place the API base URL is hard-coded."*

```json
{
  "pattern": "https://api\\.example\\.com",
  "path_scope": "D:\\work\\my-app",
  "exts": ["ts", "tsx", "js"],
  "fixed_strings": false,
  "max_results": 100
}
```

### 8.3 Duplicate detection

*"Do I have duplicate files in this photo library?"*

```json
{"mode": "name_size", "path_scope": "D:\\work\\photos", "min_size": 1048576, "max_groups": 10}
```

Escalate to `{"mode": "hash", "hash_algo": "sha256", "max_hash_bytes": 536870912}`
when you need certainty.

### 8.4 Recent changes

*"What did I touch today?"*

```json
{"action": "run", "name": "today_changed", "max_results": 50}
```

or, scoped to a project:

```json
{"since": "today", "path_scope": "D:\\work\\my-app", "max_results": 50}
```

### 8.5 Large-file audit

*"Where did my disk space go?"* Two calls: aggregate by extension, then list the
biggest files.

```json
{"query": "", "path_scope": "D:\\", "group_by": "ext", "top_n": 15, "max_scan": 50000}
```

```json
{"query": "size:>500mb", "sort": "size", "ascending": false, "max_results": 20,
 "fields": ["name", "path", "size"]}
```

### 8.6 Cluster by extension or directory

```json
{"group_by": "ext", "path_scope": "D:\\work\\my-app", "top_n": 20}
```

```json
{"group_by": "dir", "dir_depth": 1, "path_scope": "D:\\work\\my-app", "top_n": 20}
```

`group_by=size_bucket` gives a histogram, `month` groups by modification month,
`parent` by immediate parent directory.

### 8.7 Export to CSV

*"Give me every log over 10 MB as a spreadsheet."*

```json
{
  "query": "ext:log size:>10mb",
  "out_path": "reports/big_logs.csv",
  "format": "csv",
  "sort": "size",
  "ascending": false,
  "max_results": 5000
}
```

The file lands in `EV_EXPORT_ROOT`; `rows`, `bytes` and `truncated` tell you whether
you need another slice with `offset`.

### 8.8 Custom presets

Set `EV_PRESETS_FILE` to a JSON file (a template is in
[`examples/presets.json`](examples/presets.json)):

```json
{
  "presets": [
    {
      "name": "recent_downloads_7d",
      "description": "Files modified in the last 7 days in Downloads",
      "query": "dm:last7days",
      "path_scope": "{downloads}",
      "sort": "date_modified",
      "ascending": false,
      "max_results": 100,
      "fields": ["name", "path", "size", "date_modified"]
    }
  ]
}
```

Then `{"action": "run", "name": "recent_downloads_7d"}`. Presets override
built-ins by name; placeholders `{downloads}`, `{userprofile}` and `{workspace}`
expand at runtime. JSON uses only the standard library; YAML needs PyYAML.

### 8.9 Download a file

```json
{"path": "D:\\work\\builds\\release.zip", "dest": "artifacts/release.zip", "max_bytes": 104857600}
```

The destination resolves under `EV_DOWNLOAD_ROOT`; the call fails cleanly if the
file grew beyond `max_bytes` or the destination escapes the root. This copies
bytes over loopback HTTP — it is not a search operation.

## 9. Benchmarks

![Query latency on C:\Program Files](benchmarks/charts/latency_programfiles.png)

![Speedup over the native recursive scan](benchmarks/charts/speedup.png)

![Scaling with corpus size](benchmarks/charts/scaling.png)

Measured 2026-10-18 on Windows 10 Pro (AMD Ryzen 7 5800H, 16 GB RAM), Everything 1.4.1.1024 with 3,243,750 index entries, Python 3.11.7. Native baseline: warm PowerShell 7.6.6 in one `-NoProfile` process; MCP: in-process call to the function behind each tool. Median of 3 samples (2 for `C:\Program Files`), one warm-up discarded; speedup = native scan ÷ MCP call. The run used `EV_SCAN_CAP=600000`; total runtime was 517 s.

**C:\Windows\System32** — 17,616 files, 25,531 index entries

| Scenario | Native scan (ms) | MCP (ms) | Speedup | Counts match |
|---|---:|---:|---:|:---:|
| Name glob | 510 | 1.0 | 519× | no |
| Name regex | 812 | 65.7 | 12.4× | yes |
| Type + size + date filter | 796 | 0.8 | 1,034× | no |
| Bytes by extension | 760 | 346 | 2.2× | no |
| Top-N largest | 774 | 0.9 | 885× | yes |
| Files >= 100 MB | 693 | 1.3 | 514× | no |
| Changed in last 7 days | 669 | 0.9 | 780× | no |

**C:\Program Files** — 427,637 files, 554,843 index entries

| Scenario | Native scan (ms) | MCP (ms) | Speedup | Counts match |
|---|---:|---:|---:|:---:|
| Name glob | 14,465 | 0.9 | 15,504× | no |
| Type + size + date filter | 20,805 | 12.8 | 1,626× | no |
| Bytes by extension | 22,804 | 7,959 | 2.9× | no |
| Top-N largest | 27,435 | 8.7 | 3,154× | yes |
| Files >= 100 MB | 21,336 | 17.9 | 1,189× | no |

**synthetic corpus (30k files / 200 dirs)** — 30,000 files, 30,201 index entries

| Scenario | Native scan (ms) | MCP (ms) | Speedup | Counts match |
|---|---:|---:|---:|:---:|
| Name glob | 179 | 1.1 | 162× | yes |
| Name regex | 797 | 72.3 | 11.0× | yes |
| Type + size + date filter | 438 | 1.0 | 453× | yes |
| Bytes by extension | 554 | 526 | 1.1× | yes |
| Top-N largest | 724 | 0.9 | 792× | yes |
| Files >= 100 MB | 412 | 16.1 | 25.6× | yes |
| Duplicate groups | 549 | 560 | 1.0× | yes |
| Changed in last 7 days | 440 | 15.9 | 27.7× | no |
| Content grep | 21,001 | 1,354 | 15.5× | yes |
| Single small directory | 3.0 | 0.7 | 4.1× | yes |

The MCP side was measured as an in-process call, so these timings exclude JSON-RPC transport: a separate end-to-end MCP stdio round-trip measured 5.74 ms median — add that for whole-call latency. Grouping and duplicate detection are the weak spot for the MCP side (about 1–3×) because they paginate the index over HTTP 1000 rows at a time. The 200-file directory case is near-parity in absolute terms: 3.0 ms versus 0.7 ms.

**Why counts differ.** Rows marked `no` are legitimate differences, not errors: (a) ACL-protected subtrees such as `C:\Windows\System32\config\systemprofile` and `C:\Program Files\WindowsApps` are indexed by Everything but cannot be enumerated by a normal-user recursive scan — this is the largest difference and it makes the *index* counts higher; (b) Windows system directories churn, so some index entries lag the disk; (c) Everything's `dm:` filter is day-granular while the native baseline used an exact now-7×24h window; (d) `size:` units are KB-quantized. Counts are presented as information, not as a correctness gate — the query semantics and returned rows are what matter.

### 9.1 Methodology

The benchmark harness lives in [`benchmarks/`](benchmarks/) and is reproducible:
`benchmarks/bench.py` drives both sides, `benchmarks/native_baseline.ps1` contains
the PowerShell baseline, and `benchmarks/gen_dataset.py` generates the synthetic
corpus.

- **Same corpus, same query, both sides.** Each scenario runs against an identical
  file set: a native PowerShell recursive scan and the equivalent Everything index
  query, with the same path scope.
- **Native baseline is warm, idiomatic PowerShell 7.6.6.** `Get-ChildItem`,
  `Where-Object` and `findstr` run inside one `pwsh -NoProfile` process timed with
  `[Diagnostics.Stopwatch]`. Shell startup is excluded, one warm-up pass is
  discarded, and each scenario reports the median of 3 samples (2 samples for the
  16–27-second `C:\Program Files` pass). Grouping uses an O(n) hashtable rather
  than `Group-Object`, because PowerShell 5.1's `Group-Object` took 36 s on 30k
  items — the baseline is idiomatic, not artificially slow. This is the *best case*
  for a scan.
- **The MCP side is the same code path, in-process.** Each measurement calls the
  exact function behind the MCP tool with the same query and the same path scope; a
  separate end-to-end stdio measurement reports JSON-RPC overhead (5.74 ms median on
  the reference machine).
- **Correctness is checked, not assumed.** Native and MCP hit counts are compared
  per scenario; legitimate mismatches (result caps, index scope, ACL-protected
  subtrees, KB-quantized `size:` syntax, day-granular `dm:`) are recorded in
  `count_match` / `count_note` and explained above rather than hidden.
- **Corpora:** `C:\Windows\System32` (small, ~17.6k files), `C:\Program Files`
  (large, ~427.6k files), and a generated synthetic tree (30k files, 200
  directories, known extension/size/mtime distribution, ~350 duplicate-related
  files). The complete run used `EV_SCAN_CAP=600000` so aggregation scenarios could
  cover all of `C:\Program Files`; every other setting was default.

### 9.2 Fairness caveats — read this before quoting a number

> **Honest summary:** these numbers compare a *prebuilt index query* with a *live
> recursive scan*. They measure the value of having an index, not a magic faster
> filesystem. On small or already-cached scopes the gap shrinks to near parity;
> on large corpora and repeated queries it is large. Everything is Windows/NTFS
> only, and `everything_grep` speed comes from ripgrep plus a small candidate set,
> not from the index.

- **The index is prebuilt and maintained.** Everything's advantage comes from
  having already walked the filesystem once and tracking changes via the USN
  journal. The benchmark compares a query against that index with a live scan; it
  does not compare indexing cost with scan cost. Someone still pays to build and
  maintain the index.
- **Cold caches and tiny scopes erase the advantage.** On a small directory whose
  metadata is already in RAM, a native scan can finish in a few milliseconds; the
  MCP round trip plus HTTP query may be comparable or slower. The speedup grows
  with corpus size and with repeated queries.
- **Windows and NTFS only.** Everything cannot index Linux filesystems, network
  shares (by default), FAT/exFAT volumes, or files outside the volumes you add to
  it. There is no portable equivalent here.
- **Content search is ripgrep, not Everything.** `everything_grep` speed depends on
  the candidate tree, file sizes, ignore rules and the pattern — not on the
  Everything index. It is still far faster than grepping a whole disk because the
  candidate set is small.
- **Aggregation and duplicate detection are the weakest case for the MCP side.**
  `everything_summary` and `everything_duplicates` page through the index 1000 rows
  per HTTP request, so their speedup is roughly 1–3× rather than the three-digit
  numbers of a single filtered query. They are bounded and predictable, but they are
  not where the index shines.
- **Index memory and disk are real costs.** Everything keeps the index in memory
  and on disk (hundreds of MB on a multi-million-entry machine). The benchmark
  numbers do not include that footprint.
- **Tool-call overhead is excluded from per-operation timings** except in the
  explicitly reported stdio round-trip measurement. An agent pays that overhead on
  every call.

## 10. Architecture

```text
┌──────────────────────────┐
│ MCP client (DSH / other) │
│  mcp__everything__*      │
└────────────┬─────────────┘
             │ MCP stdio (JSON-RPC over stdin/stdout)
┌────────────▼─────────────┐
│ server.py                │  FastMCP server: 7 query tools + 3 resources,
│ everything_mcp/server.py │  registers 6 composite tools
└────────────┬─────────────┘
             │
     ┌───────┴────────┐
     │                │
┌────▼─────────┐  ┌───▼──────────────────────────────────────────────┐
│ client.py    │  │ compose_tools.py                                 │
│ search/count │  │ summary · duplicates · grep · batch_stat ·       │
│ status/export│  │ changes · reveal                                 │
│ download     │  │        │                                         │
└────┬─────────┘  │        ├── rg.py → ripgrep (content search)       │
     │            │        └── local read-only post-processing        │
     │            └───┬──────────────────────────────────────────────┘
┌────▼─────────┐      │
│ http_channel │      │
│ urllib, JSON │      │
└────┬─────────┘      │
     │ HTTP GET http://127.0.0.1:8090/?search=...&json=1
┌────▼─────────────────▼─────┐
│ voidtools Everything       │
│ built-in HTTP JSON API     │
│ NTFS index (USN journal)   │
└────────────────────────────┘
```

A tool call flows: MCP client → FastMCP dispatcher → tool function → `client.py`
(parameter validation, query composition, warnings) → `http_channel.py` (URL
encoding, HTTP, FILETIME/size normalization) → Everything. Composite tools page
through `client.search` and post-process locally; `everything_grep` shells out to
ripgrep instead of reading file contents in Python.

| Module | Responsibility |
|---|---|
| `server.py` | Entry point; works from any cwd |
| `everything_mcp/server.py` | FastMCP instance, 7 query tools, 3 MCP resources, composite-tool registration |
| `everything_mcp/client.py` | Business layer: search/count/status/export/download, `path_scope`, `fields`, warnings, auto-start |
| `everything_mcp/http_channel.py` | Everything HTTP client, JSON parsing, FILETIME→ISO 8601, download URL construction |
| `everything_mcp/compose_tools.py` | The 6 composite tools and their budgets/caps |
| `everything_mcp/rg.py` | ripgrep detection and content-search wrapper |
| `everything_mcp/presets.py` | Built-in presets, `EV_PRESETS_FILE` loading and validation |
| `everything_mcp/help.py` | Capability-matrix constants and `everything_help` rendering |
| `everything_mcp/winipc.py` | Process/window probing and exe version reading (diagnostics only, ctypes) |
| `everything_mcp/config.py` | Environment-variable configuration |
| `tests/test_mcp.py` | End-to-end MCP test suite |
| `scripts/setup.ps1` | Read-only environment doctor, optional ini writer |
| `scripts/privacy_check.py` | Pre-publish personal-data scan |
| `benchmarks/` | Native-vs-MCP benchmark harness, results and charts |
| `docs/` | Usage cookbook and capability matrix |
| `examples/` | DSH profile patch, generic MCP config, custom presets |

## 11. Security and privacy

- **Loopback only.** The server talks to `http://127.0.0.1:8090/` and refuses
  nothing else; it never contacts the internet. No telemetry, no analytics, no
  update check.
- **Everything's HTTP API has no authentication.** Binding it to `0.0.0.0` or a LAN
  address exposes the entire file listing and file downloads to the network.
  Always use `http_server_bindings=127.0.0.1`. This is the single most important
  security setting in the whole setup.
- **Read-only by default.** Searching, counting, summarizing, duplicate detection,
  batch stat, changes and grep only read. The only writes are
  `everything_export` and `everything_download`, both confined to
  `EV_EXPORT_ROOT` / `EV_DOWNLOAD_ROOT` after path resolution and symlink checks;
  `everything_download` enforces `max_bytes` and removes partial files.
- **One side-effect tool.** `everything_reveal` launches Explorer. Set
  `EV_ALLOW_SIDE_EFFECTS=0` for a strictly read-only server; it is re-read on every
  call, so no restart is needed.
- **Bounded responses.** `EV_MAX_RESULTS_CAP` (1000), `EV_SCAN_CAP` (50k),
  `EV_HASH_MAX_BYTES` (256 MiB), grep timeouts, a 4 KiB duplicate response budget
  and 10k-row export cap keep a single call from dumping a disk into the model
  context or pinning the machine.
- **No file contents leave the machine.** The server is local; the model only sees
  the JSON returned by tools.
- **Auto-start is local and visible.** It launches `Everything.exe -startup`
  minimized and only when no Everything process is running and the HTTP endpoint is
  unreachable. Set `EV_AUTO_START=0` to manage Everything yourself.
- **No credentials.** Nothing here reads or stores secrets, tokens or keys.

## 12. Limitations

- **Windows + NTFS only.** No macOS/Linux support, and Everything does not index
  network shares or FAT/exFAT volumes by default.
- **Needs a running Everything with its HTTP server enabled.** Without it the tools
  return an actionable error; there is no fallback scanner.
- **1.5 is not verified.** The capability matrix (supported syntax, slow queries,
  column behaviour) was measured on **1.4.1.1024**. Everything 1.5 changes some
  query functions; re-run `tests/test_mcp.py` and the matrix commands after an
  upgrade.
- **`dc:` / `da:` / `attrib:` are not indexed by default** and can exceed 5
  seconds. Enabling them requires ini changes and a full index rebuild.
- **Hour-level relative dates do not exist in 1.4.** `everything_changes`
  normalizes to date-level `dm:` and filters precisely client-side; `total` is a
  date-level candidate count.
- **`size:` with units is KB-quantized**, not byte-exact (see §7.3).
- **Content search depends on ripgrep** being installed or bundled. If it is
  missing, `everything_grep` fails with instructions; the other 12 tools are
  unaffected.
- **`everything_duplicates(mode=name)` uses `dupe:`**, which scans the whole index
  (~200 ms on a 3.2M-entry machine). It is much slower than a normal query.
- **No IPC/ETP transport.** Everything 1.4's IPC query protocol needs the official
  SDK; this project deliberately uses only the built-in HTTP server. IPC window
  probing is used only to detect whether Everything is running.
- **Named Everything instances are unsupported.** `-instance` / `-config`
  instances cannot use the Everything service (empty index), so the server always
  reuses the default instance.
- **Aggregate numbers can be lower bounds** when `max_scan` / `EV_SCAN_CAP` is hit;
  the `truncated_scan` flag says so.
- **Index freshness is near-real-time, not instant.** Files created by other
  processes are typically visible in 1–5 seconds; deletions disappear on the same
  order. A file just written may not be searchable for a moment.

## 13. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `everything_status` says HTTP is unreachable | HTTP server disabled, wrong port, or bindings typo | Set `http_server_enabled=1`, `http_server_port=8090`, `http_server_bindings=127.0.0.1`, restart Everything completely |
| HTTP never starts, no error | `http_server_bindings` written as `127.0.0.1:8090` | Bindings take a **bare IP**; the port is the separate `http_server_port` key |
| Port 8090 answers but it is not Everything | Another program owns the port | `netstat -ano \| findstr :8090`, check the PID, change both `http_server_port` and `EV_HTTP_PORT` |
| Search returns 0 but Everything works | Index still building, or the query uses an unsupported prefix | Check `everything_status.index.total_items`; try a plain name search; verify the prefix in `everything_help` |
| `everything_grep` reports ripgrep not found | `rg.exe` not in `PATH` / DSH bundle missing | `winget install BurntSushi.ripgrep.MSVC`, or set `EV_RG_EXE`; the error lists probed paths |
| `everything_reveal` returns `ok=false` | `EV_ALLOW_SIDE_EFFECTS=0` | Set it to `1` (or remove it); no restart required |
| `everything_download` returns 404 | File moved/deleted, index stale, or file downloads disabled | Re-run `everything_search`; ensure `http_server_allow_file_download=1` |
| `everything_download` reports too large | File exceeds `max_bytes` (default 2 GiB) | Raise `max_bytes` or choose another transfer method |
| Export/download rejected as outside the root | Destination resolves outside `EV_EXPORT_ROOT` / `EV_DOWNLOAD_ROOT` | Use a relative path under the root, or change the env variable |
| `size:<1kb` finds almost nothing | 1.4 `size:` is KB-quantized | Use unitless bytes: `size:<1024` |
| `dc:` / `da:` / `attrib:` queries time out | Those fields are not indexed | Don't use them, or enable the ini switches and rebuild the index |
| Chinese file names not found | Old Everything version, or a non-UTF-8 intermediary | Use this server's tools (UTF-8 over HTTP); require Everything 1.4+ |
| Named instance shows an empty index | Named instances cannot use the Everything service | Use the default instance |
| Auto-start does not happen | `EV_AUTO_START=0`, or `Everything.exe` path not found | Set `EV_AUTO_START=1` and/or `EV_EVERYTHING_EXE` |
| Every DSH tool call fails after editing the profile | Stale fibers from a live profile edit | Restart `dsh web`; the error is `cannot get required service "sessionProjections" in inactive context` |
| Queries time out on a busy disk | Index rebuild, huge page, or large download | Raise `EV_HTTP_TIMEOUT` (e.g. `5`) and retry |
| `python -m pip install -e .` fails | pip too old, or Python < 3.10 | Upgrade pip and use Python 3.10+ |

## 14. Development and tests

```powershell
git clone https://github.com/CZM1998/dsh_everything.git
cd dsh_everything
python -m pip install -e .
python tests\test_mcp.py
```

The test suite is an end-to-end MCP test: it starts the server over stdio (or falls
back to anonymous-pipe / in-memory transport when the environment forbids named
pipes), performs the initialize handshake, lists tools and resources, and exercises
every tool against the real local index.

- **142 assertion checks**, all expected green on a machine with Everything
  running. Tier A checks (handshake, registration, status, search/count shapes,
  export and path-escape rejection, a bad-port error, both entry points,
  help/status/presets/resources) do not depend on the index being fully built.
- **Tier B** waits up to 240 s for the index and skips rather than fails on timeout
  (fresh installs can still be indexing).
- **Temporary data** is created with Python inside the workspace so Everything
  indexes it within seconds; tests then delete it and poll `count == 0` to confirm
  the index caught up. Do not create fixtures with tools whose writes Everything
  does not observe immediately and then assert on index visibility.
- The suite never starts or stops Everything (`EV_AUTO_START=0` inside the test)
  and never modifies the user's index.
- Useful test environment variables: `EV_HTTP_PORT`, `EV_TEST_INDEX_WAIT`,
  `EV_TEST_TRANSPORT=auto|stdio|memory`.

Before publishing changes, run the privacy check:

```powershell
python scripts\privacy_check.py          # files known to git
python scripts\privacy_check.py --all    # whole working tree
```

It flags real user-profile paths, e-mail addresses, mobile numbers and any terms in
`.privacy-blocklist` / `PRIVACY_TERMS`.

Repository layout:

```text
dsh_everything/
├── server.py                  # entry point (any cwd)
├── everything_mcp/            # the package
│   ├── server.py              # FastMCP server: query tools + MCP resources
│   ├── client.py              # business layer: search/count/status/export/download
│   ├── http_channel.py        # Everything HTTP client + normalization
│   ├── compose_tools.py       # 6 composite tools
│   ├── rg.py                  # ripgrep detection + content search
│   ├── presets.py             # built-in + custom presets
│   ├── help.py                # capability matrix + help rendering
│   ├── winipc.py              # process/window diagnostics (ctypes)
│   └── config.py              # environment configuration
├── tests/test_mcp.py          # end-to-end MCP suite (142 assertion checks)
├── scripts/                   # setup doctor + privacy check
├── docs/                      # usage cookbook, capability matrix
├── examples/                  # profile patch, MCP config, presets
├── benchmarks/                # native-vs-MCP harness, results, charts
├── pyproject.toml             # packaging (console script dsh-everything-mcp)
├── LICENSE                    # MIT
└── README.md / README.zh-CN.md
```

The project has no third-party runtime dependencies beyond the MCP SDK, uses only
the Python standard library otherwise, and logs to stderr so it never corrupts the
stdio protocol stream. Contributions are welcome — keep the capability matrix and
tests in sync with any behavior change.

## 15. License and acknowledgements

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 CZM1998.

Built on the shoulders of:

- [voidtools Everything](https://www.voidtools.com/) — the index and HTTP API that
  make this whole approach possible.
- [ripgrep](https://github.com/BurntSushi/ripgrep) — fast content search behind
  `everything_grep`.
- [Model Context Protocol](https://modelcontextprotocol.io/) and the
  [official Python SDK](https://github.com/modelcontextprotocol/python-sdk) —
  transport and server framework.
- [DeepSeek Harness](https://github.com/deepseek-ai/dsh) — the MCP host this
  server was designed for.

Everything is a trademark of voidtools. This project is not affiliated with or
endorsed by voidtools.
