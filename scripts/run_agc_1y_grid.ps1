param(
    [string]$DataRoot = "data/agc-bybit-1y",
    [string]$Start = "2025-05-01",
    [string]$End = "2026-05-01",
    [int]$Workers = 0,
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
    & $Python -c "import sys; print(sys.version); raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
}

$SymbolCsv = $Symbols -join ","
$Datasets = "instruments,klines_1h"
$Config = "configs/volume_alpha.default.yaml"

Invoke-Checked "Downloading one-year Bybit research data into $DataRoot" {
    & $Python -m aggression_carry --data-root $DataRoot --config $Config download-data --symbols $SymbolCsv --start $Start --end $End --datasets $Datasets
}

Invoke-Checked "Writing one-year volume alpha sweep" {
    & $Python -m aggression_carry --data-root $DataRoot --config $Config volume-alpha
}

Invoke-Checked "Writing one-year concurrent volume grid" {
    & $Python -m aggression_carry --data-root $DataRoot --config $Config volume-grid --workers $Workers --include-reverse
}

Write-Host ""
Write-Host "Done."
Write-Host "Volume alpha report: $DataRoot/reports/volume_alpha_report.md"
Write-Host "Grid report: $DataRoot/reports/volume_grid_report.md"
Write-Host "Grid CSV: $DataRoot/reports/volume_grid_results.csv"
