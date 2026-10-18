# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the project adheres to [Semantic Versioning](https://semver.org/).

## [2.0.0] - 2026-10-18

First public release.

### Added

- Everything (voidtools) HTTP JSON API client with channel selection, automatic
  launch and health checking.
- 13 MCP tools:
  `everything_search`, `everything_count`, `everything_status`,
  `everything_help`, `everything_presets`, `everything_export`,
  `everything_download`, `everything_summary`, `everything_duplicates`,
  `everything_grep`, `everything_batch_stat`, `everything_changes`,
  `everything_reveal`.
- 3 MCP resources: `everything://help/syntax`, `everything://status`,
  `everything://presets`.
- Aggregation helpers: group-by-extension, group-by-directory, size buckets,
  duplicate detection by name/size/hash.
- ripgrep-backed content search that reuses the Everything index to pick roots
  instead of walking disks.
- Named presets (built-in + user JSON/YAML file), CSV/JSONL/EFU export, and
  filesystem-confined download.
- Reproducible benchmark harness comparing native PowerShell scans with
  index-backed queries (`benchmarks/`), including generated charts.
- `scripts/setup.ps1` environment doctor and `scripts/privacy_check.py`
  pre-publish scanner.
- Full test suite (142 assertions) covering the HTTP channel, every tool and
  the compose layer.

### Security

- HTTP access is loopback-only by default (`127.0.0.1:8090`); the server never
  contacts the network on its own.
- All query tools are read-only. Side-effecting tools (`reveal`, `download`,
  `export`) are gated by `EV_ALLOW_SIDE_EFFECTS` and confined to configured
  roots.

## [0.1.0] - 2026-09

- Internal prototype: search / count / status over the Everything HTTP API.

[2.0.0]: https://github.com/CZM1998/dsh_everything/releases/tag/v2.0.0
