# Usage cookbook

Task-oriented recipes for `dsh_everything`: what to call, in which order, and what
the response should look like. For installation see the [README](../README.md);
for the verified query matrix see [CAPABILITIES.md](CAPABILITIES.md).

**English** | [简体中文](USAGE.zh-CN.md)

- In DSH, every tool name is prefixed with `mcp__everything__`
  (`mcp__everything__everything_search`). This document uses the bare names for
  readability.
- All arguments are JSON objects passed to the tool. Paths, sizes and timings in
  the examples are illustrative — use your own paths.
- Responses are trimmed to the fields that matter. Real responses also contain
  `effective_query`, `channel`, `elapsed_ms`, `stats` and `warnings`.
- Before anything looks wrong, call `everything_status`; when a query syntax is in
  doubt, call `everything_help`.

## Contents

- [Conventions](#conventions)
- [Recipes](#recipes)
  - [1. Find a file by name fragment](#1-find-a-file-by-name-fragment)
  - [2. Search inside one project](#2-search-inside-one-project)
  - [3. Filter by type, size and date](#3-filter-by-type-size-and-date)
  - [4. Existence checks and counts](#4-existence-checks-and-counts)
  - [5. Top-N largest files](#5-top-n-largest-files)
  - [6. Aggregate by extension](#6-aggregate-by-extension)
  - [7. Aggregate by directory](#7-aggregate-by-directory)
  - [8. Duplicate files](#8-duplicate-files)
  - [9. Content search with ripgrep](#9-content-search-with-ripgrep)
  - [10. Recent changes](#10-recent-changes)
  - [11. Export to CSV / JSONL / EFU](#11-export-to-csv--jsonl--efu)
  - [12. Download a file](#12-download-a-file)
  - [13. Reveal a path in Explorer](#13-reveal-a-path-in-explorer)
  - [14. Presets](#14-presets)
  - [15. Batch metadata and hashes](#15-batch-metadata-and-hashes)
- [Agent prompt patterns](#agent-prompt-patterns)
- [Troubleshooting scenarios](#troubleshooting-scenarios)

## Conventions

| Concept | Convention |
|---|---|
| Workspace | `<WORKSPACE>` is the agent working directory (`EV_WORKSPACE_ROOT`). Examples use `D:\work`. |
| Repository | `<PATH-TO-REPO>` is where this project was cloned. |
| Paging | Pass the returned `next_offset` as the next `offset`; stop when `truncated=false`. |
| Field projection | Request only the `fields` you need — it reduces tokens and skips unused Everything columns. |
| Counts first | For expensive composite tools, call `everything_count` first to size the job. |
| Read-only | Only `everything_export`, `everything_download` and `everything_reveal` have side effects. |

## Recipes

### 1. Find a file by name fragment

**When:** the user remembers part of a name but not the location.

**Call**

```json
{
  "query": "\"Q3 budget\" ext:xlsx",
  "sort": "date_modified",
  "ascending": false,
  "max_results": 20,
  "fields": ["name", "path", "size", "date_modified"]
}
```

**Expected shape**

```json
{
  "total": 3,
  "returned": 3,
  "truncated": false,
  "next_offset": 3,
  "results": [
    {"type": "file", "name": "Q3-budget-2026.xlsx",
     "path": "D:\\work\\reports\\Q3-budget-2026.xlsx",
     "size": 48213, "date_modified": "2026-10-12T08:21:04Z"}
  ],
  "warnings": []
}
```

**Notes:** quotes force a phrase; without them the words are AND-ed. Use `wfn:` for
whole-name matches, `ext:` with semicolons for several extensions. If the user does
not know the extension, drop `ext:` — do not guess one.

### 2. Search inside one project

**When:** scope matters more than the pattern.

**Call**

```json
{"query": "ext:ts", "path_scope": "D:\\work\\my-app", "max_results": 5}
```

**Expected shape**

```json
{
  "total": 214,
  "returned": 5,
  "truncated": true,
  "next_offset": 5,
  "results": [{"name": "server.ts", "path": "D:\\work\\my-app\\src\\server.ts"}]
}
```

**Notes:** `path_scope` is added to the Everything query as `path:"..."` *and*
checked client-side, so results cannot leak outside the scope. Prefer `path_scope`
over embedding `path:` in `query` — it is easier for the model to get right.

### 3. Filter by type, size and date

**When:** "big old logs", "recent installers", "large video files".

**Call**

```json
{
  "query": "ext:log size:>10mb dm:>=2026-09-01",
  "sort": "size",
  "ascending": false,
  "max_results": 20,
  "fields": ["name", "path", "size", "date_modified"]
}
```

**Expected shape**

```json
{
  "total": 137,
  "results": [
    {"name": "build.log", "path": "D:\\work\\builds\\build.log",
     "size": 53485308, "date_modified": "2026-09-14T06:18:42Z"}
  ]
}
```

**Notes:** `size:` with `kb/mb/gb` is KB-quantized — see
[README §7](../README.md#7-everything-query-syntax-cheat-sheet). For exact
thresholds use bytes: `size:>10485760`. `dm:` is modification time; `dc:` and
`da:` are not indexed by default and are very slow.

### 4. Existence checks and counts

**When:** you only need "is it there?" or "how many?" — the cheapest calls.

**Calls**

```json
{"query": "wfn:important.docx"}
```

```json
{"query": "ext:py", "path_scope": "D:\\work\\my-app", "folder_only": false}
```

**Expected shape**

```json
{"query": "wfn:important.docx", "total": 2, "elapsed_ms": 3.1, "channel": "http"}
```

**Notes:** `everything_count` never returns file paths, making it the right choice
for a decision (`total > 0`) before reading or processing anything.

### 5. Top-N largest files

**When:** disk-space audit.

**Call**

```json
{
  "query": "size:>500mb",
  "sort": "size",
  "ascending": false,
  "max_results": 20,
  "fields": ["name", "path", "size"]
}
```

**Expected shape**

```json
{
  "total": 96,
  "results": [
    {"name": "image.iso", "path": "D:\\work\\images\\image.iso", "size": 4294967296}
  ]
}
```

**Notes:** `sort=size&ascending=false` puts the biggest first. Add `ext:iso` or
`path_scope` to narrow. If you need total bytes rather than a list, use
`everything_summary` with `group_by=size_bucket` (or sum the returned sizes).

### 6. Aggregate by extension

**When:** "what kinds of files are in here / what is using the space?"

**Call**

```json
{"query": "", "path_scope": "D:\\work\\my-app", "group_by": "ext", "top_n": 10}
```

**Expected shape**

```json
{
  "total": 2000,
  "scanned": 2000,
  "truncated_scan": false,
  "groups": [
    {"key": "py", "count": 421, "size": 1820011, "min_size": 120, "max_size": 88213, "avg_size": 4323},
    {"key": "md", "count": 88, "size": 441002, "min_size": 210, "max_size": 9120, "avg_size": 5011}
  ],
  "totals": {"count": 2000, "size": 4200000, "min": 0, "max": 91200}
}
```

**Notes:** only files are aggregated by default (`size` would be meaningless for
directories). `truncated_scan=true` means the numbers are a lower bound — raise
`max_scan` (bounded by `EV_SCAN_CAP`, default 50000) or narrow the scope.

### 7. Aggregate by directory

**When:** finding which subdirectory is heavy.

**Call**

```json
{
  "query": "",
  "path_scope": "D:\\work\\my-app",
  "group_by": "dir",
  "dir_depth": 1,
  "top_n": 20
}
```

**Expected shape**

```json
{
  "groups": [
    {"key": "(root)", "count": 12, "size": 22000},
    {"key": "src", "count": 180, "size": 920000},
    {"key": "node_modules", "count": 900, "size": 3100000}
  ],
  "dir_grouping": "relative_to_path_scope"
}
```

**Notes:** with a `path_scope`, keys are relative to it; without one they are tails
of absolute paths. `dir_depth` ranges 1–12. `group_by=parent` groups by immediate
parent directory instead.

### 8. Duplicate files

**When:** cleaning downloads, photos, backups.

**Call (fast signal)**

```json
{"mode": "name_size", "path_scope": "D:\\work\\photos", "min_size": 1048576, "max_groups": 10}
```

**Call (confirmation)**

```json
{"mode": "hash", "query": "", "path_scope": "D:\\work\\photos",
 "min_size": 1048576, "hash_algo": "sha256", "max_hash_bytes": 536870912}
```

**Expected shape**

```json
{
  "mode": "name_size",
  "groups": [
    {"key": "IMG_0001.jpg|2048", "count": 2, "size": 2048, "path_count": 2,
     "paths": ["D:\\work\\photos\\backup\\IMG_0001.jpg",
               "D:\\work\\photos\\IMG_0001.jpg"]}
  ],
  "stats": {"groups_found": 1, "returned_groups": 1, "dupe_support": true},
  "warnings": []
}
```

**Notes:** `name_size` is a fast heuristic; `hash` is definitive but reads bytes.
Only the first `max_paths_per_group` paths are returned (default 2); `path_count` is
the true count. The tool never deletes anything — deleting is the caller's decision
and should be confirmed with the user.

### 9. Content search with ripgrep

**When:** searching *inside* files (`TODO`, error strings, API keys, config keys).

**Call**

```json
{
  "pattern": "TODO|FIXME",
  "path_scope": "D:\\work\\my-app",
  "exts": ["py", "ts"],
  "max_results": 50,
  "case_sensitive": false
}
```

**Expected shape**

```json
{
  "matches": [
    {"path": "D:\\work\\my-app\\backend\\server.py", "line": 42,
     "column": 5, "text": "# TODO: cache this"}
  ],
  "stats": {"files_searched": 214, "files_matched": 3, "elapsed_ms": 118.2,
            "truncated": false, "timed_out": false}
}
```

**Notes:** this is ripgrep, not the Everything index. Narrow the candidate set with
`exts`, `globs` (`["!vendor/**"]`) or `path_scope`. `fixed_strings=true` for literal
text. For a pattern like a URL, escape regex characters or use `fixed_strings`.

### 10. Recent changes

**When:** "what changed today / this week", stand-up summaries, reviewing generated
output.

**Call**

```json
{"since": "7d", "path_scope": "D:\\work\\my-app", "max_results": 50}
```

**Expected shape**

```json
{
  "items": [
    {"name": "server.py", "path": "D:\\work\\my-app\\backend\\server.py",
     "date_modified": "2026-10-18T03:15:07Z", "extension": "py"}
  ],
  "count": 12, "total": 33,
  "window": {"since": "2026-10-11T03:27:59Z", "until": "2026-10-18T03:27:59Z",
             "spec": "7d", "dm_expression": "dm:>=2026-10-11", "precise": true},
  "warnings": ["<explains that the dm: filter is date-level and the exact window is applied client-side>"]
}
```

**Notes:** `total` is the date-level candidate count; `count` is exact. `since`
accepts `today`, `24h`, `7d`, `30m`, `last7days` or an ISO timestamp. Sort defaults
to newest first.

### 11. Export to CSV / JSONL / EFU

**When:** handing results to a human, a spreadsheet, or another tool.

**Call**

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

**Expected shape**

```json
{"ok": true, "format": "csv", "path": "D:\\work\\reports\\big_logs.csv",
 "rows": 137, "bytes": 24512, "total": 137, "truncated": false}
```

**Notes:** the path resolves against `EV_EXPORT_ROOT`; anything outside it is
rejected. CSV is UTF-8 with BOM (Excel-friendly). `jsonl` is one JSON object per
line; `efu` is an Everything file list. Default 5000 rows, cap 10000 — page with
`offset` for more. Return the saved path to the user; do not paste thousands of
rows into the conversation.

### 12. Download a file

**When:** the agent needs the file locally (in the workspace) rather than just its
path.

**Call**

```json
{"path": "D:\\work\\builds\\release.zip", "dest": "artifacts/release.zip"}
```

**Expected shape**

```json
{"ok": true, "source": "D:\\work\\builds\\release.zip",
 "saved_path": "D:\\work\\artifacts\\release.zip",
 "bytes": 33554432, "max_bytes": 2147483648}
```

**Notes:** requires `http_server_allow_file_download=1`. `dest` resolves under
`EV_DOWNLOAD_ROOT`; a collision gets a numeric suffix. `max_bytes` defaults to 2
GiB. A 404 means the file moved or the index is stale — search again first.

### 13. Reveal a path in Explorer

**When:** the user is at the machine and wants the file opened.

**Call**

```json
{"path": "D:\\work\\reports\\Q3-budget-2026.xlsx"}
```

**Expected shape**

```json
{"ok": true, "opened": true, "mode": "select", "path": "D:\\work\\reports\\Q3-budget-2026.xlsx"}
```

**Notes:** this is the only side-effect tool. In read-only deployments
(`EV_ALLOW_SIDE_EFFECTS=0`) it returns `ok=false` with a hint and launches nothing.
Never use it in a loop.

### 14. Presets

**When:** repeated queries, or a user-defined "usual suspects" list.

**Calls**

```json
{"action": "list"}
```

```json
{"action": "run", "name": "large_files_100mb", "max_results": 30}
```

**Expected shape**

```json
{
  "action": "run",
  "name": "large_files_100mb",
  "preset": {"query": "size:>100mb", "sort": "size", "ascending": false},
  "result": {"total": 96, "returned": 30, "results": []},
  "warnings": []
}
```

**Notes:** eight built-ins ship with the server; add your own with
`EV_PRESETS_FILE` (see [README §8.8](../README.md#88-custom-presets)). Presets
support `{downloads}`, `{userprofile}` and `{workspace}` placeholders.

### 15. Batch metadata and hashes

**When:** after a search, checking which of N paths exist, their sizes and whether
two files are identical.

**Call**

```json
{
  "paths": ["D:\\work\\a\\report.txt", "D:\\work\\b\\report.txt", "D:\\work\\gone.txt"],
  "include_hash": true,
  "hash_algo": "sha256"
}
```

**Expected shape**

```json
{
  "items": [
    {"path": "D:\\work\\a\\report.txt", "exists": true, "size": 2048,
     "hash": "9f2c…", "date_modified": "2026-10-12T08:21:04Z"},
    {"path": "D:\\work\\gone.txt", "exists": false, "error": "not found"}
  ],
  "exists_count": 2, "missing_count": 1
}
```

**Notes:** at most 200 paths per call (extras are truncated with a warning). Hashes
are read-only and bounded by `max_hash_bytes`. Use this instead of N separate shell
`stat` calls.

## Agent prompt patterns

These are prompts a user can give a DSH agent, and the tool sequence the agent
should follow. Tool prefixes are omitted for readability.

### "Find the Q3 report and summarize it"

1. `everything_search` with `{"query": "\"Q3 report\"", "sort": "date_modified", "ascending": false, "max_results": 10}`
2. If several hits, pick the newest `.docx`/`.xlsx`/`.md`; if none, retry with
   `ext:` variants or a looser phrase.
3. Read the chosen file with the normal file-reading tool, then summarize.
4. Answer with the exact path and the summary. If the file is ambiguous, list the
   candidates and ask one clarifying question.

### "What changed in this project today?"

1. `everything_changes` with `{"since": "today", "path_scope": "<PROJECT>", "max_results": 50}`.
2. Group the returned items by directory or extension in the answer.
3. If the user cares about *content* changes, follow up with `everything_grep` on
   the relevant files, or read the diffs with the normal shell tools.

### "Clean up duplicate files in my photos folder"

1. `everything_count` with `{"query": "", "path_scope": "D:\\work\\photos"}` to size the job.
2. `everything_duplicates` with `{"mode": "name_size", "path_scope": "D:\\work\\photos", "min_size": 1048576}`.
3. For candidate groups, escalate to `{"mode": "hash", "hash_algo": "sha256"}` if the
   user needs certainty.
4. Present groups and proposed deletions; **never delete without explicit
   confirmation**. `everything_duplicates` itself only reads.

### "Find every place the API key is used"

1. `everything_count` with a broad name filter if the user knows a file type; then
2. `everything_grep` with `{"pattern": "API_KEY", "path_scope": "<PROJECT>", "exts": ["py", "ts", "js", "env"], "max_results": 100}`.
3. Report file:line for each match; do not print secret values into the transcript.
4. If ripgrep is missing, relay the actionable error (install or `EV_RG_EXE`).

### "Export all build logs over 10 MB"

1. `everything_count` with `{"query": "ext:log size:>10mb"}` to confirm the size.
2. `everything_export` with `{"query": "ext:log size:>10mb", "out_path": "reports/big_logs.csv", "format": "csv", "max_results": 5000}`.
3. If `truncated=true`, export additional slices with `offset` (cap 10000/export).
4. Return the saved path and row count, not the rows themselves.

### "Why is my disk full?"

1. `everything_summary` with `{"query": "", "path_scope": "D:\\", "group_by": "ext", "top_n": 15, "max_scan": 50000}`.
2. `everything_search` with `{"query": "size:>500mb", "sort": "size", "ascending": false, "max_results": 20}`.
3. Optionally `{"group_by": "size_bucket"}` for a histogram.
4. Summarize the top contributors; note `truncated_scan` when numbers are lower bounds.

## Troubleshooting scenarios

### A. Every tool call errors, or `status` says the endpoint is unreachable

1. Call `everything_status`. Read `http.reachable`, `http.error`,
   `everything.running` and `hints`.
2. If `http.reachable=false` but `everything.running=true`: the HTTP server is off,
   on another port, or mis-bound. Re-check the ini keys. Remember:
   `http_server_bindings` must be a **bare IP** — `127.0.0.1:8090` silently fails.
3. If `everything.running=false`: start Everything, or set `EV_AUTO_START=1`.
4. If the port answers but `everything.running=false`, another program owns 8090:
   `netstat -ano | findstr :8090`, then change both the ini port and `EV_HTTP_PORT`.

### B. Queries succeed but return nothing

1. `everything_status` → `index.total_items`. `0` means the index is still building
   or a named instance has no service-backed index; wait or use the default instance.
2. Try a trivial name search (`{"query": "*.exe", "max_results": 1}`). If that works,
   the problem is query semantics, not the index.
3. Check the prefix in `everything_help`: `distinct:`, `sort:`, `run_count:` and
   `date_run:` are 1.5-only and return 0 on 1.4. `ext:a,b` and `ext:py*` also
   return 0 — use semicolons and exact extensions.
4. If it is a `size:` query, remember the KB quantization
   ([README §7.3](../README.md#73-size--the-kb-quantization-trap)).

### C. `everything_grep` cannot find ripgrep

1. The error lists `env`, `dsh-bundled`, `PATH` and candidate locations it tried.
2. Install ripgrep (`winget install BurntSushi.ripgrep.MSVC`) or set `EV_RG_EXE` to
   an existing `rg.exe`.
3. Restart the MCP server (or DSH) so the new environment variable is picked up.
4. Other tools are unaffected — continue with `everything_search`.

### D. `everything_download` fails

1. 404 → the file moved, was deleted, or the index is stale. Re-run
   `everything_search`, then retry with the fresh path.
2. 403 / no content → `http_server_allow_file_download=1` is not set.
3. "too large" → raise `max_bytes` (default 2 GiB) or choose a different transfer.
4. "outside the download root" → `dest` resolves outside `EV_DOWNLOAD_ROOT`; use a
   relative path under it.
5. Timeout → raise `EV_HTTP_TIMEOUT` and retry once.

### E. A file written moments ago is not found

Everything updates from the NTFS USN journal within seconds, but not instantly.
Wait 1–5 seconds and retry. Do not build workflows that require zero-latency
read-after-write through the index; read the file directly when you already have
its path.

### F. Export or download rejected as outside the workspace

The destination resolves outside `EV_EXPORT_ROOT` / `EV_DOWNLOAD_ROOT`. Either use
a relative path (which is resolved against the root) or change the environment
variable. This check is deliberate: side-effect tools must not be able to write
anywhere on the disk.

### G. `everything_reveal` refuses

`EV_ALLOW_SIDE_EFFECTS=0` is set. It is re-read on every call: set it to `1` and
retry — no restart needed. If the path does not exist, the tool reports an
actionable error instead of opening Explorer.
