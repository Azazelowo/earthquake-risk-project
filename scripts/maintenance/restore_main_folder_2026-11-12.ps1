# Undo the 2026-11-12 reorganisation of the main project folder: moves every
# item listed in the move log back to its original path. Nothing is deleted
# or overwritten.
#
# This script must sit next to the move log, i.e. in
#   <main folder>\_archive\2026-11-12_*\
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File restore.ps1          # dry run, prints what would move
#   powershell -ExecutionPolicy Bypass -File restore.ps1 -Apply   # actually move
#
# The log is the *.csv in this folder whose header has 9 columns
# (the other CSV is the per-file SHA256 list). Columns are read by position
# so this file needs no non-ASCII literals (Windows PowerShell 5.1 safe).
param([switch]$Apply)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$logFile = Get-ChildItem -LiteralPath $PSScriptRoot -Filter '*.csv' |
    Where-Object { ((Get-Content -LiteralPath $_.FullName -TotalCount 1 -Encoding UTF8) -split ',').Count -eq 9 } |
    Select-Object -First 1
if (-not $logFile) { throw 'move log CSV not found' }
$rows = @(Import-Csv -LiteralPath $logFile.FullName -Encoding UTF8)
[array]::Reverse($rows)
foreach ($r in $rows) {
    $v = @($r.PSObject.Properties | ForEach-Object { $_.Value })
    $orig = Join-Path $root $v[1]   # column 2: original relative path
    $now  = Join-Path $root $v[2]   # column 3: current relative path
    if (-not (Test-Path -LiteralPath $now))  { Write-Host "SKIP (not at new path): $($v[2])"; continue }
    if (Test-Path -LiteralPath $orig)        { Write-Host "SKIP (original path occupied): $($v[1])"; continue }
    if ($Apply) {
        $parent = Split-Path $orig -Parent
        if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force $parent | Out-Null }
        Move-Item -LiteralPath $now -Destination $orig
        Write-Host "RESTORED: $($v[1])"
    } else {
        Write-Host "WOULD RESTORE: $($v[2])  ->  $($v[1])"
    }
}
if (-not $Apply) { Write-Host 'Dry run only. Re-run with -Apply to move files back.' }
