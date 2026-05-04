param(
    [string]$DataRoot = "data/daily-close-fade-pit-20230503-20260503",
    [string]$Config = "configs/volume_alpha.default.yaml",
    [string]$Start = "2023-05-03",
    [string]$End = "2026-05-03",
    [int]$ManifestWorkers = 16,
    [int]$DownloadWorkers = 16,
    [int]$MaxSymbols = 0,
    [int]$MaxRows = 0,
    [int]$MinBarsPerDay = 1200,
    [string]$Symbols = "",
    [switch]$SkipManifest,
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
    $Python = Join-Path $RepoRoot ".venv/bin/python"
}
if (-not (Test-Path $Python)) {
    throw "Virtualenv not found. Run scripts/windows_setup.ps1 on Windows or create .venv on macOS/Linux first."
}

$LogDir = Join-Path $RepoRoot (Join-Path $DataRoot "logs")
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $LogDir "archive_pit_bootstrap_$Stamp.log"

Start-Transcript -Path $LogPath -Append | Out-Null
try {
    Write-Host "Repo: $RepoRoot"
    Write-Host "Data root: $DataRoot"
    Write-Host "Window: $Start to $End"
    Write-Host "Manifest workers: $ManifestWorkers"
    Write-Host "Download workers: $DownloadWorkers"
    Write-Host "Max symbols: $MaxSymbols"
    Write-Host "Max rows: $MaxRows"
    Write-Host "Log: $LogPath"

    Invoke-Checked "Checking virtualenv Python version" {
        & $Python -c "import sys; print(sys.executable); print(sys.version); raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    }

    if (-not $SkipManifest) {
        Invoke-Checked "Building Bybit archive manifest" {
            & $Python -m aggression_carry `
                --data-root $DataRoot `
                --config $Config `
                archive-manifest `
                --name "pit_manifest" `
                --start $Start `
                --end $End `
                --quote-suffix USDT `
                --symbols $Symbols `
                --max-symbols $MaxSymbols `
                --workers $ManifestWorkers
        }
    }
    else {
        Write-Host "Skipping archive manifest because -SkipManifest was provided."
    }

    if (-not $SkipDownload) {
        Invoke-Checked "Downloading archive-derived 1m klines" {
            & $Python -m aggression_carry `
                --data-root $DataRoot `
                --config $Config `
                archive-download-klines `
                --name "pit_klines" `
                --start $Start `
                --end $End `
                --symbols $Symbols `
                --max-rows $MaxRows `
                --workers $DownloadWorkers
        }
    }
    else {
        Write-Host "Skipping archive kline download because -SkipDownload was provided."
    }

    Invoke-Checked "Auditing PIT coverage" {
        & $Python .\scripts\report_archive_pit_coverage.py `
            --data-root $DataRoot `
            --start $Start `
            --end $End `
            --min-bars-per-day $MinBarsPerDay
    }

    Write-Host ""
    Write-Host "Done."
    Write-Host "Coverage: $DataRoot/reports/archive_pit_coverage_report.md"
    Write-Host "Manifest: $DataRoot/reports/archive_manifest_pit_manifest.md"
    Write-Host "Download: $DataRoot/reports/archive_klines_pit_klines.md"
    Write-Host "Log: $LogPath"
}
finally {
    Stop-Transcript | Out-Null
}
