param(
    [string]$DataRoot = "data/daily-close-fade-1m-3m",
    [string]$UniverseRoot = "data/universe-research",
    [string]$UniverseName = "top160-current",
    [string]$Start = "2026-02-03",
    [string]$End = "2026-05-03",
    [int]$Workers = 8,
    [int]$RankEnd = 160,
    [int]$MaxSymbols = 160,
    [switch]$SkipDownload
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

function Invoke-Checked {
    param(
        [string]$Name,
        [scriptblock]$Command
    )

    Write-Host ""
    Write-Host "==> $Name"
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE."
    }
}

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Virtualenv not found. Run .\scripts\windows_setup.ps1 first."
}

$Config = "configs/volume_alpha.default.yaml"
$LogDir = Join-Path $RepoRoot (Join-Path $DataRoot "logs")
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $LogDir "daily_close_fade_1m_$Stamp.log"

Start-Transcript -Path $LogPath -Append | Out-Null
try {
    Write-Host "Repo: $RepoRoot"
    Write-Host "Data root: $DataRoot"
    Write-Host "Window: $Start to $End"
    Write-Host "Workers: $Workers"
    Write-Host "Log: $LogPath"

    Invoke-Checked "Checking virtualenv Python version" {
        & $Python -c "import sys; print(sys.executable); print(sys.version); raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    }

    Invoke-Checked "Discovering current Bybit universe" {
        & $Python -m aggression_carry `
            --data-root $UniverseRoot `
            --config $Config `
            discover-universe `
            --name $UniverseName `
            --rank-start 1 `
            --rank-end $RankEnd `
            --max-symbols $MaxSymbols `
            --min-turnover-24h 2000000 `
            --min-age-days 10 `
            --include-majors
    }

    $SymbolPath = Join-Path $RepoRoot (Join-Path $UniverseRoot "reports\universe_$UniverseName`_symbols.txt")
    if (-not (Test-Path $SymbolPath)) {
        throw "Universe symbol file not found: $SymbolPath"
    }
    $SymbolCsv = (Get-Content $SymbolPath -Raw).Trim()
    if (-not $SymbolCsv) {
        throw "Universe symbol file is empty: $SymbolPath"
    }

    if (-not $SkipDownload) {
        Invoke-Checked "Downloading resumable 1m Bybit klines" {
            & $Python -m aggression_carry `
                --data-root $DataRoot `
                --config $Config `
                download-data `
                --symbols $SymbolCsv `
                --start $Start `
                --end $End `
                --datasets "instruments,klines_1m"
        }
    }
    else {
        Write-Host "Skipping download because -SkipDownload was provided."
    }

    Invoke-Checked "Running daily-close-fade 1m grid" {
        & $Python -m aggression_carry `
            --data-root $DataRoot `
            --config $Config `
            daily-close-fade-grid `
            --workers $Workers
    }

    Write-Host ""
    Write-Host "Done."
    Write-Host "Report: $DataRoot/reports/daily_close_fade_grid_report.md"
    Write-Host "CSV: $DataRoot/reports/daily_close_fade_grid_results.csv"
    Write-Host "Log: $LogPath"
}
finally {
    Stop-Transcript | Out-Null
}
