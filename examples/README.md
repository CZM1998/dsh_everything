# Examples

Ready-to-copy configuration snippets for `dsh_everything`. Nothing here contains
machine-specific paths: replace the placeholders before use.

| File | Purpose |
|---|---|
| [`cordis.patch.yml`](cordis.patch.yml) | DSH profile patch entry that registers this server over MCP stdio |
| [`mcp.json`](mcp.json) | Generic MCP client config (Claude Desktop / MCP Inspector style) |
| [`presets.json`](presets.json) | Example `EV_PRESETS_FILE` with useful custom presets |

## Placeholders

| Placeholder | Replace with | Example |
|---|---|---|
| `<PYTHON>` | Full path to `python.exe` (3.10+) | `C:\Python311\python.exe` |
| `<PATH-TO-REPO>` | Directory where you cloned this repository | `D:\work\dsh_everything` |
| `<WORKSPACE>` | Directory the agent may read and write | `D:\work` |

Use `where python` to find the interpreter path. If Python is already on `PATH`
you can also use the string `python` instead of a full path.

## `cordis.patch.yml` (DSH)

1. Copy the single list item into `%USERPROFILE%\.dsh\profiles\<profile>\cordis.patch.yml`
   (for `dsh web` the profile is usually `web`). The file is a top-level YAML array,
   so merge the item into the existing array rather than creating a second one.
2. Replace the placeholders.
3. Restart `dsh web`. Editing a profile while a session is running can leave stale
   fibers and make every tool call fail with
   `cannot get required service "sessionProjections" in inactive context`;
   a restart fixes it.
4. Ask the agent to call `mcp__everything__everything_status`. When the tools appear,
   the registration worked.

`serverName: everything` is what produces the `mcp__everything__*` tool prefix.
Keep `failOnStartupError: false` so a broken MCP server cannot block harness startup.

## `mcp.json` (other MCP clients)

Use this shape for clients that read the common `mcpServers` object. For DSH, use
`cordis.patch.yml` instead — DSH profiles are YAML patches, not JSON.

## `presets.json` (custom presets)

Point `EV_PRESETS_FILE` at this file (absolute path, or a path relative to
`EV_WORKSPACE_ROOT`). Presets are merged over the eight built-in ones and can
override them by name.

```text
EV_PRESETS_FILE=<PATH-TO-REPO>\examples\presets.json
```

Then:

```json
{"action": "list"}                                   // see all presets
{"action": "run", "name": "large_videos_500mb"}       // run one
```

Supported fields per preset: `name`, `description`, `query`, `regex`,
`path_scope`, `sort`, `ascending`, `max_results`, `fields`. The file may be either
`{"presets": [...]}` or a bare JSON array. YAML works too when PyYAML is installed.

Preset text supports three placeholders, expanded at runtime:

| Placeholder | Expands to |
|---|---|
| `{downloads}` | current user's `Downloads` folder |
| `{userprofile}` | current user's profile directory |
| `{workspace}` | `EV_WORKSPACE_ROOT` |

Keep custom queries to Everything 1.4 syntax (`ext:`, `size:`, `dm:`, `path:`,
`parent:`, `wfn:`, …). Do not use `distinct:`, `sort:`, `run_count:` or
`date_run:` — those are Everything 1.5 features and return nothing on 1.4.
See [`../docs/CAPABILITIES.md`](../docs/CAPABILITIES.md) for the verified matrix.
