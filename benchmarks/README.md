# Benchmarks

Reproducible comparison of **native Windows file operations** against **the
Everything index exposed by this MCP server**.

Everything maintains a persistent NTFS index of names, paths, sizes and
modification times. A query against that index is O(matches), while a recursive
scan is O(corpus) and pays the directory walk every time. These benchmarks
quantify that difference for the file operations an agent actually performs.

## What is measured

| Side | How |
|---|---|
| **native** | Idiomatic PowerShell 5.1 / 7 (`Get-ChildItem`, `Where-Object`, `findstr`) executed inside one `pwsh -NoProfile` process. Timing is `[Diagnostics.Stopwatch]` around the whole pipeline, one warm-up pass discarded, median of N samples. Shell startup, profile loading and cold-cache effects are excluded — this is the **best case** for the scan approach. |
| **mcp** | The exact Python function each MCP tool wraps (`client.count`, `client.search`, `compose_tools.summary_impl`, …), called in-process, same query and same path scope, median of N samples. |
| **stdio overhead** | One end-to-end MCP stdio round-trip (`initialize` + N × `tools/call`) so the JSON-RPC/process boundary can be added to the in-process numbers. |

Both sides see the same corpus and answer the same question. Result counts are
recorded for both sides (`count_match`), and any difference carries a
`count_note` explaining it instead of being hidden.

## Corpora

| id | scope | files | indexed entries |
|---|---|---|---|
| `system32` | `C:\Windows\System32` | 17,616 | ~25.5k |
| `programfiles` | `C:\Program Files` | 384,463 | ~555k |
| `synthetic` | `benchmarks/.data` (generated, git-ignored) | 30,000 in 200 dirs | ~30.2k |

The synthetic corpus is deterministic (seed `20261018`): 1.51 GiB logical,
8 extensions, lognormal sizes 1 KiB–50 MiB, five files > 100 MiB, ~300
intentional duplicate groups (848 duplicate files), 350 files containing
`benchmark-token`, mtimes spread over two years with a slice inside the last
seven days. It lets the duplicate/content scenarios run on known ground truth.

`dataset_manifest.json` records the generated distribution; `.data/` itself is
never committed.

## Scenarios

| id | question |
|---|---|
| `name_glob` | find `*.dll` by name (synthetic: `*.log`) |
| `name_regex` | regex filename match |
| `multi_filter` | extension **and** size **and** mtime filter |
| `cluster_ext` | group by extension: file count (+ bytes) |
| `topn_largest` | ten largest files |
| `big_file_stats` | files ≥ 100 MB: count and total size |
| `duplicates` | same-size duplicate groups |
| `recent_changes` | files modified in the last 7 days |
| `content_grep` | search a token inside files (`findstr` vs ripgrep) |
| `small_scope` | the same search in a single 200-file directory |

Not every scenario runs on every corpus: the two multi-minute PowerShell passes
on `C:\Program Files` are limited to the five most representative scenarios, and
`duplicates`/`content_grep` use the synthetic corpus where ground truth is known.
Per-corpus scenario lists live in `bench.py` (`CORPUS_SCENARIOS`).

## Run it yourself

```powershell
# 1. one-time: generate the synthetic corpus (~2 min, ~1.5 GiB sparse-ish)
python benchmarks/gen_dataset.py --force

# 2. full run: all corpora, 3 reps, charts  (~10-15 min on a Ryzen 7 5800H)
python benchmarks/bench.py --corpus all --reps 3

# quick subset
python benchmarks/bench.py --corpus synthetic --reps 2
python benchmarks/bench.py --corpus system32 --reps 5 --no-charts

# native side only (raw JSON)
powershell -NoProfile -File benchmarks/native_baseline.ps1 `
    -Root "C:\Windows\System32" -Reps 3
```

Requirements: Everything 1.4.1+ running with its HTTP server on
`127.0.0.1:8090` (see the top-level README), Python 3.10+ with `mcp`, and
PowerShell (Windows PowerShell 5.1 is enough; PowerShell 7 is fine too). The
harness is read-only and writes only under `results/` and `charts/`.

## Outputs

| Path | Contents |
|---|---|
| `results/latest.json` | stable schema: `meta`, `corpora`, `scenarios` (median ms, speedup, both counts, samples) |
| `results/summary.csv` | flat table for spreadsheets |
| `charts/latency_programfiles.png` | native vs MCP latency per scenario (log scale) |
| `charts/speedup.png` | speed-up factor per scenario |
| `charts/scaling.png` | latency vs corpus size for one scenario (O(N) vs ~flat) |

## Reading the numbers honestly

- **The index is prebuilt.** Everything already paid the cost of building and
  maintaining its index (USN journal). A cold index, a newly created file, or a
  volume Everything does not index (FAT/exFAT, network share) has no advantage.
- **Native is measured warm and idiomatic.** Its numbers are not a strawman: one
  process, no shell startup, cached metadata, O(n) hashtable aggregation for the
  grouping scenarios (PowerShell 5.1's `Group-Object` is pathologically slow —
  36 s for 30k items — and would have made the baseline look worse than it is).
- **Aggregation is the weak spot for the MCP side.** `summary`/`duplicates` fetch
  rows in 1000-item pages over HTTP, so their speed-up is small (~1–3×). Pick the
  tool based on the job: index queries win big for *finding and filtering*;
  one-pass aggregation over the whole corpus is closer to parity.
- **Content search is ripgrep, not Everything.** The index is only used to pick
  the roots; the bytes are read by `rg`. Findstr is a slow baseline, so the
  content-grep speed-up reflects ripgrep as much as the index.
- **Tiny scopes show no advantage.** In a single 200-file directory, process and
  HTTP overhead dominate and both sides are within a few milliseconds.
- **Counts can differ legitimately.** The index and a directory walk disagree on
  junctions, hidden/system files, files changed after the USN entry, and
  Everything's KB-quantized `size:` semantics. Every mismatch in
  `results/latest.json` carries a `count_note`.

Hardware and software versions are recorded in `meta` of the results file;
results are only comparable between runs on similar hardware.
