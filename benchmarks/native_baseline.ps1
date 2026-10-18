#Requires -Version 5.1
<#
.SYNOPSIS
    Native Windows baseline for the dsh_everything benchmarks.

.DESCRIPTION
    Runs the *idiomatic* native equivalent of each benchmark scenario against one
    corpus root and prints one JSON document to stdout:

        { "root": "...", "reps": 3, "scenarios": [
            { "id": "name_glob", "samples_ms": [..], "median_ms": 123.4,
              "count": 42, "extra": { .. } }, ... ] }

    Timing uses [Diagnostics.Stopwatch] around the whole pipeline, with one
    warm-up pass discarded, so shell startup and (after the warm-up) file-system
    cache misses are excluded. This is intentionally the *best case* for the
    scan-based approach.

.PARAMETER Root
    Directory to scan (same scope the Everything query is restricted to).

.PARAMETER Scenarios
    Comma-separated scenario ids. Unknown ids are skipped.

.EXAMPLE
    powershell -NoProfile -File benchmarks/native_baseline.ps1 -Root 'C:\Windows\System32' -Reps 3

.NOTES
    Windows PowerShell 5.1 or PowerShell 7. Read-only: nothing is written.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [string]$Scenarios = 'name_glob,name_regex,multi_filter,cluster_ext,topn_largest,big_file_stats,recent_changes,duplicates,content_grep,small_scope',
    [int]$Reps = 3,
    [string]$Token = 'benchmark-token',
    [string]$NameGlob = '*.dll',
    [string]$NameRegex = '^api-ms-win-.*-l1-.*\.dll$',
    [string]$FilterExts = '.exe,.dll',
    [long]$FilterMinBytes = 1048576
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

if (-not (Test-Path -LiteralPath $Root)) {
    throw "corpus root does not exist: $Root"
}

function Get-MedianMs {
    param([double[]]$Values)
    if (-not $Values -or $Values.Count -eq 0) { return $null }
    $sorted = @($Values | Sort-Object)
    $mid = [int][math]::Floor($sorted.Count / 2)
    if ($sorted.Count % 2 -eq 1) {
        return [math]::Round([double]$sorted[$mid], 2)
    }
    return [math]::Round(([double]$sorted[$mid - 1] + [double]$sorted[$mid]) / 2.0, 2)
}

$gci = @{
    LiteralPath   = $Root
    Recurse       = $true
    File          = $true
    Force         = $true
    ErrorAction   = 'SilentlyContinue'
}
$smallDir = Join-Path $Root 'small_scope'
$since7d  = (Get-Date).AddDays(-7)
$minDate  = [datetime]::new(2024, 1, 1)
$extSet   = @($FilterExts -split ',' | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ })

# Each block returns either an int (file/hit count) or a hashtable with a
# 'count' key plus optional extras. Only stream/file objects are produced.
#
# Note on Group-Object: Windows PowerShell 5.1 implements it with a very slow
# pipeline (36 s for 30k objects were measured here), which would make the
# baseline look artificially bad. The hashtable aggregation below is the same
# O(n) work a practitioner would write when scanning hundreds of thousands of
# files, so it is used for cluster_ext and duplicates.
$blocks = @{
    name_glob = {
        @(Get-ChildItem @gci -Filter $NameGlob).Count
    }

    name_regex = {
        @(Get-ChildItem @gci | Where-Object { $_.Name -match $NameRegex }).Count
    }

    multi_filter = {
        @(Get-ChildItem @gci | Where-Object {
            ($extSet -contains $_.Extension.ToLowerInvariant()) -and
            $_.Length -gt $FilterMinBytes -and
            $_.LastWriteTime -ge $minDate
        }).Count
    }

    cluster_ext = {
        $byExt = @{}
        $files = 0
        foreach ($item in (Get-ChildItem @gci)) {
            $key = $item.Extension.ToLowerInvariant()
            $slot = $byExt[$key]
            if ($null -eq $slot) {
                $byExt[$key] = @{ count = 1; bytes = $item.Length }
            } else {
                $slot.count++
                $slot.bytes += $item.Length
            }
            $files++
        }
        $bytes = 0
        foreach ($slot in $byExt.Values) { $bytes += $slot.bytes }
        @{ count = $files; groups = $byExt.Count; bytes = $bytes }
    }

    topn_largest = {
        # Sort-Object -Top does a partial sort and keeps the ten largest.
        @(Get-ChildItem @gci | Sort-Object -Property Length -Descending -Top 10).Count
    }

    big_file_stats = {
        $big = @(Get-ChildItem @gci | Where-Object { $_.Length -ge 100MB })
        $bytes = 0
        foreach ($item in $big) { $bytes += $item.Length }
        @{ count = $big.Count; bytes = $bytes }
    }

    duplicates = {
        $bySize = @{}
        foreach ($item in (Get-ChildItem @gci)) {
            $key = [string]$item.Length
            $slot = $bySize[$key]
            if ($null -eq $slot) {
                $bySize[$key] = @{ count = 1; size = $item.Length }
            } else {
                $slot.count++
            }
        }
        $groups = 0
        $files = 0
        foreach ($slot in $bySize.Values) {
            if ($slot.count -gt 1) {
                $groups++
                $files += $slot.count
            }
        }
        @{ count = $groups; files = $files }
    }

    recent_changes = {
        @(Get-ChildItem @gci | Where-Object { $_.LastWriteTime -ge $since7d }).Count
    }

    content_grep = {
        # Resolve findstr explicitly: the PATH inherited from an agent sandbox is
        # not guaranteed to contain %SystemRoot%\System32.
        $findstr = Join-Path $env:SystemRoot 'System32\findstr.exe'
        if (-not (Test-Path -LiteralPath $findstr)) { $findstr = 'findstr.exe' }
        Push-Location -LiteralPath $Root
        try {
            $hits = @(& $findstr /s /i /m /c:"$Token" '*')
        } finally {
            Pop-Location
        }
        $hits.Count
    }

    small_scope = {
        @(Get-ChildItem -LiteralPath $smallDir -File -Force -Filter '*.txt' -ErrorAction SilentlyContinue).Count
    }
}

$requested = @($Scenarios -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })

$results = New-Object System.Collections.Generic.List[object]
foreach ($id in $requested) {
    if (-not $blocks.ContainsKey($id)) {
        [Console]::Error.WriteLine("unknown scenario: $id")
        continue
    }
    $block = $blocks[$id]

    # Warm-up pass: fills the file-system cache and JITs the pipeline; discarded.
    $null = & $block

    $samples = New-Object 'System.Collections.Generic.List[double]'
    $value = $null
    for ($rep = 0; $rep -lt $Reps; $rep++) {
        $watch = [System.Diagnostics.Stopwatch]::StartNew()
        $value = & $block
        $watch.Stop()
        $samples.Add([math]::Round($watch.Elapsed.TotalMilliseconds, 2))
    }

    $count = $null
    $extra = @{}
    if ($value -is [hashtable]) {
        $count = $value['count']
        if ($null -ne $value['groups']) { $extra['groups'] = $value['groups'] }
        if ($null -ne $value['files'])  { $extra['files']  = $value['files'] }
        if ($null -ne $value['bytes'])  { $extra['bytes']  = $value['bytes'] }
    } else {
        $count = $value
    }

    $results.Add([pscustomobject]@{
        id         = $id
        samples_ms = $samples.ToArray()
        median_ms  = (Get-MedianMs $samples.ToArray())
        count      = $count
        extra      = $extra
    })
}

[pscustomobject]@{
    root      = $Root
    reps      = $Reps
    scenarios = $results.ToArray()
} | ConvertTo-Json -Depth 8 -Compress
