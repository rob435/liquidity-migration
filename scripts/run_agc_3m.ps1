param(
    [string]$DataRoot = "data/agc-bybit-3m",
    [string]$Start = "2025-01-01",
    [string]$End = "2025-04-01",
    [switch]$KeepRawTrades,
    [string[]]$Symbols = @(
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "XRPUSDT",
        "DOGEUSDT",
        "LINKUSDT",
        "AVAXUSDT",
        "APTUSDT",
        "BNBUSDT",
        "ADAUSDT",
        "DOTUSDT",
        "LTCUSDT",
        "NEARUSDT",
        "OPUSDT",
        "ARBUSDT",
        "INJUSDT"
    )
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

function Invoke-Checked {
    param(
        [string]$Name,
        [scriptblock]$Command
    )

    Write-Host $Name
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE."
    }
}

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Virtualenv not found. Run .\scripts\windows_setup.ps1 first."
}

Invoke-Checked "Checking virtualenv Python version" {
    & $Python -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)"
}

$SymbolCsv = $Symbols -join ","
$Datasets = "instruments,klines_1h,klines_5m,funding,open_interest,ticker_snapshots,archive_trades"
$ArchiveTemplate = "https://public.bybit.com/trading/{symbol}/{symbol}{date}.csv.gz"
$Config = "configs/aggression_carry.default.yaml"
$DownloadArgs = @(
    "-m", "aggression_carry",
    "--data-root", $DataRoot,
    "--config", $Config,
    "download-data",
    "--symbols", $SymbolCsv,
    "--start", $Start,
    "--end", $End,
    "--datasets", $Datasets,
    "--archive-url-template", $ArchiveTemplate
)

if (-not $KeepRawTrades) {
    $DownloadArgs += "--skip-raw-public-trades"
}

Invoke-Checked "Downloading Bybit research data into $DataRoot" {
    & $Python @DownloadArgs
}
Invoke-Checked "Building features" {
    & $Python -m aggression_carry --data-root $DataRoot --config $Config build-features
}
Invoke-Checked "Writing alpha report" {
    & $Python -m aggression_carry --data-root $DataRoot --config $Config alpha-report
}
Invoke-Checked "Writing portfolio backtest" {
    & $Python -m aggression_carry --data-root $DataRoot --config $Config portfolio-backtest
}

Write-Host ""
Write-Host "Done."
Write-Host "Alpha report: $DataRoot/reports/alpha_report.md"
Write-Host "Portfolio report: $DataRoot/reports/portfolio_backtest.md"
