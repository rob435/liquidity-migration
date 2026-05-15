param(
    [string]$DataRoot = "",
    [ValidateSet("smoke", "quick", "promotion", "legacy")]
    [string]$Preset = "promotion",
    [int]$Workers = 16,
    [string]$Python = "python",
    [string]$Config = "",
    [string]$ReportDir = "",
    [switch]$SkipPull
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if ($DataRoot -eq "") {
    $DataRoot = Join-Path $RepoRoot "data\agc-bybit-3y-auto150-20230503-20260503"
}
if ($Config -eq "") {
    $Config = Join-Path $RepoRoot "configs\volume_alpha.default.yaml"
}
if ($ReportDir -eq "") {
    $RunTag = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
    $ReportDir = Join-Path (Join-Path $DataRoot "reports") "volume_grid_splits_${Preset}_${RunTag}"
}

function Invoke-Checked {
    param(
        [string]$File,
        [string[]]$Arguments
    )

    & $File @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$File failed with exit code $LASTEXITCODE"
    }
}

function Invoke-Logged {
    param(
        [string]$File,
        [string[]]$Arguments,
        [string]$LogPath
    )

    $writer = [System.IO.StreamWriter]::new($LogPath, $false)
    try {
        & $File @Arguments 2>&1 | ForEach-Object {
            $line = "$_"
            Write-Host $line
            $writer.WriteLine($line)
        }
        $exitCode = $LASTEXITCODE
    }
    finally {
        $writer.Dispose()
    }
    return $exitCode
}

Set-Location $RepoRoot

if (-not (Test-Path $DataRoot)) {
    throw "Data root not found: $DataRoot"
}
if (-not (Test-Path $Config)) {
    throw "Config not found: $Config"
}

if (-not $SkipPull) {
    Invoke-Checked "git" @("checkout", "main")
    Invoke-Checked "git" @("fetch", "origin")
    Invoke-Checked "git" @("pull", "--ff-only", "origin", "main")
}

$env:VOLUME_GRID_BACKEND = "thread"
$env:POLARS_MAX_THREADS = "1"
$env:RAYON_NUM_THREADS = "1"
$env:OMP_NUM_THREADS = "1"
$env:MKL_NUM_THREADS = "1"

New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

$metadata = @(
    "repo_root=$RepoRoot",
    "data_root=$DataRoot",
    "config=$Config",
    "report_dir=$ReportDir",
    "preset=$Preset",
    "workers=$Workers",
    "volume_grid_backend=$env:VOLUME_GRID_BACKEND",
    "polars_max_threads=$env:POLARS_MAX_THREADS",
    "rayon_num_threads=$env:RAYON_NUM_THREADS"
)
$metadata | Set-Content -Path (Join-Path $ReportDir "run.meta.txt") -Encoding UTF8

$gridLog = Join-Path $ReportDir "run.grid.log"
$promotionLog = Join-Path $ReportDir "run.promotion.log"
$splitSummary = Join-Path $ReportDir "volume_grid_split_summary.csv"
$promotionDir = Join-Path $ReportDir "promotion"

Write-Host "Starting volume overnight grid"
Write-Host "Report dir: $ReportDir"
Write-Host "Grid log: $gridLog"

$gridArgs = @(
    "scripts/run_volume_grid_splits.py",
    "--data-root", $DataRoot,
    "--config", $Config,
    "--preset", $Preset,
    "--workers", "$Workers",
    "--report-dir", $ReportDir
)
$gridExit = Invoke-Logged $Python $gridArgs $gridLog
if ($gridExit -ne 0) {
    throw "Volume grid failed with exit code $gridExit. See $gridLog"
}

Write-Host "Grid complete. Running promotion gate."
Write-Host "Promotion log: $promotionLog"

$promotionArgs = @(
    "scripts/evaluate_volume_promotion.py",
    "--split-summary", $splitSummary,
    "--output-dir", $promotionDir,
    "--max-worst-drawdown", "-0.35",
    "--min-avg-sharpe", "0.5"
)
$promotionExit = Invoke-Logged $Python $promotionArgs $promotionLog
if ($promotionExit -notin @(0, 2)) {
    throw "Promotion gate failed with exit code $promotionExit. See $promotionLog"
}

Write-Host "Overnight volume run complete."
Write-Host "Split report: $(Join-Path $ReportDir 'volume_grid_split_summary.md')"
Write-Host "Promotion report: $(Join-Path $promotionDir 'volume_promotion_report.md')"
if ($promotionExit -eq 2) {
    Write-Host "Promotion gate found no promotable rows."
}
