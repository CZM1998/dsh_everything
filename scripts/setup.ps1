#Requires -Version 5.1
<#
.SYNOPSIS
    Read-only environment doctor for dsh-everything-mcp.

.DESCRIPTION
    Verifies that Everything is installed and running, that its built-in HTTP
    server answers on 127.0.0.1:<Port>, that Python + the MCP SDK are available,
    and prints the exact DSH profile snippet with this machine's real paths.

    By default the script only READS. Pass -EnableHttpServer to let it write the
    three HTTP-server keys into Everything.ini (a .bak backup is created) and
    restart Everything so the change takes effect.

.EXAMPLE
    pwsh -File scripts\setup.ps1
    pwsh -File scripts\setup.ps1 -EnableHttpServer
#>
[CmdletBinding()]
param(
    [int]$Port = 8090,
    [string]$HostName = '127.0.0.1',
    [switch]$EnableHttpServer,
    [switch]$SkipPython
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$script:Problems = 0
$script:Warnings = 0

function Write-Step([string]$Text) {
    Write-Host ''
    Write-Host "== $Text" -ForegroundColor Cyan
}
function Write-Ok([string]$Text)   { Write-Host "  [ok]   $Text" -ForegroundColor Green }
function Write-Warn([string]$Text) { Write-Host "  [warn] $Text" -ForegroundColor Yellow; $script:Warnings++ }
function Write-Bad([string]$Text)  { Write-Host "  [fail] $Text" -ForegroundColor Red;    $script:Problems++ }
function Write-Info([string]$Text) { Write-Host "         $Text" -ForegroundColor DarkGray }

function Get-EverythingExe {
    $candidates = @(
        (Join-Path $env:ProgramFiles 'Everything\Everything.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Everything\Everything.exe')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    return $candidates | Select-Object -First 1
}

function Test-EverythingHttp {
    try {
        $uri = "http://${HostName}:${Port}/?search=test&count=1&json=1"
        $response = Invoke-WebRequest -Uri $uri -UseBasicParsing -TimeoutSec 4
        return ($response.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Get-IniPath {
    return Join-Path $env:APPDATA 'Everything\Everything.ini'
}

function Set-IniKey([string]$Path, [string]$Key, [string]$Value) {
    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.AddRange([System.IO.File]::ReadAllLines($Path))
    $pattern = '^\s*' + [regex]::Escape($Key) + '\s*='
    $found = $false
    for ($i = 0; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match $pattern) {
            $lines[$i] = "$Key=$Value"
            $found = $true
        }
    }
    if (-not $found) { $lines.Add("$Key=$Value") }
    [System.IO.File]::WriteAllLines($Path, $lines)
}

# ---------------------------------------------------------------------------
# 1. Everything installation + process
# ---------------------------------------------------------------------------
Write-Step 'Everything installation'
$everythingExe = Get-EverythingExe
if ($everythingExe) {
    Write-Ok "Everything.exe found: $everythingExe"
} else {
    Write-Bad 'Everything.exe not found in Program Files. Install it from https://www.voidtools.com/'
}

$everythingProcess = Get-Process -Name 'Everything' -ErrorAction SilentlyContinue
if ($everythingProcess) {
    Write-Ok ("Everything is running (PID {0})" -f ($everythingProcess.Id -join ', '))
} else {
    Write-Bad 'Everything is not running. Start it before using the MCP server (EV_AUTO_START=1 can also launch it).'
}

# ---------------------------------------------------------------------------
# 2. HTTP server
# ---------------------------------------------------------------------------
Write-Step "Everything HTTP server on http://${HostName}:${Port}/"
$httpOk = Test-EverythingHttp
if ($httpOk) {
    Write-Ok 'HTTP JSON API answered 200'
} else {
    $ini = Get-IniPath
    Write-Bad "No HTTP answer on ${HostName}:${Port}"
    if (Test-Path -LiteralPath $ini) {
        Write-Info "ini file: $ini"
    } else {
        Write-Info 'Everything.ini not found - start Everything once so it creates the file.'
    }
    Write-Info 'Required keys (Everything must be CLOSED while editing):'
    Write-Info '  http_server_enabled=1'
    Write-Info "  http_server_port=$Port"
    Write-Info "  http_server_bindings=$HostName        <-- bare IP only; 'IP:port' silently disables the server"
    if ($EnableHttpServer) {
        if (-not (Test-Path -LiteralPath $ini)) {
            Write-Bad 'Cannot edit a missing ini file.'
        } else {
            if ($everythingProcess) {
                Write-Info 'Stopping Everything to edit its ini file...'
                $everythingProcess | ForEach-Object { $_.CloseMainWindow() | Out-Null }
                Start-Sleep -Seconds 3
                Get-Process -Name 'Everything' -ErrorAction SilentlyContinue |
                    ForEach-Object { $_.WaitForExit(5000) }
            }
            $backup = "$ini.bak-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
            Copy-Item -LiteralPath $ini -Destination $backup -Force
            Write-Info "backup written: $backup"
            Set-IniKey -Path $ini -Key 'http_server_enabled' -Value '1'
            Set-IniKey -Path $ini -Key 'http_server_port' -Value "$Port"
            Set-IniKey -Path $ini -Key 'http_server_bindings' -Value $HostName
            if ($everythingExe) { Start-Process -FilePath $everythingExe | Out-Null }
            $deadline = (Get-Date).AddSeconds(30)
            while ((Get-Date) -lt $deadline -and -not (Test-EverythingHttp)) { Start-Sleep -Milliseconds 500 }
            $httpOk = Test-EverythingHttp
            if ($httpOk) { Write-Ok 'HTTP server is now answering' } else { Write-Bad 'Still no HTTP answer - restart Everything manually and re-run this script.' }
        }
    } else {
        Write-Info 'Re-run with -EnableHttpServer to apply these keys automatically.'
    }
}

# ---------------------------------------------------------------------------
# 3. Python + MCP SDK
# ---------------------------------------------------------------------------
if (-not $SkipPython) {
    Write-Step 'Python'
    $python = (Get-Command python -ErrorAction SilentlyContinue)
    if (-not $python) {
        Write-Bad 'python not found on PATH. Install Python 3.10+ and enable "Add to PATH".'
    } else {
        $version = & python --version 2>&1
        Write-Ok "$version ($($python.Source))"
        & python -c "import mcp, sys; sys.exit(0 if hasattr(mcp, 'server') or True else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Ok 'MCP SDK importable'
        } else {
            Write-Warn 'MCP SDK not importable - install it: python -m pip install "mcp>=1.2.0"'
        }
    }
}

# ---------------------------------------------------------------------------
# 4. DSH profile snippet
# ---------------------------------------------------------------------------
Write-Step 'DSH profile snippet (copy into %USERPROFILE%\.dsh\profiles\<profile>\cordis.patch.yml)'
$pythonPath = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $pythonPath) { $pythonPath = 'python' }
$serverPath = Join-Path $RepoRoot 'server.py'
@"
  - id: mcp-everything
    name: '@deepseek-ai/dsh-mcp-client'
    config:
      serverName: everything
      transport: stdio
      command: '$pythonPath'
      args: ['$serverPath']
      cwd: '$RepoRoot'
      toolCallTimeoutMs: 30000
      env:
        EV_HTTP_HOST: '$HostName'
        EV_HTTP_PORT: '$Port'
        EV_AUTO_START: '1'
        EV_WORKSPACE_ROOT: '$RepoRoot'
        EV_EXPORT_ROOT: '$RepoRoot'
        EV_DOWNLOAD_ROOT: '$RepoRoot'
        PYTHONUTF8: '1'
        PYTHONIOENCODING: 'utf-8'
"@ | Write-Host

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Step 'Summary'
if ($script:Problems -eq 0 -and $httpOk) {
    Write-Host '  Everything looks ready. Restart `dsh web` after editing the profile, then call everything_status.' -ForegroundColor Green
} else {
    Write-Host "  $script:Problems problem(s), $script:Warnings warning(s) - fix the [fail] items above." -ForegroundColor Yellow
}
if ($script:Problems -gt 0) { exit 1 }
