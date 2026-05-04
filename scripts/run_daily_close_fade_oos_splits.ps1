param(
    [string]$DataRoot = "data/daily-close-fade-1m-3y-current-top160-20230503-20260503",
    [string]$Config = "configs/volume_alpha.default.yaml",
    [string]$Splits = "train_2023_2024:2023-05-03:2024-05-03,validation_2024_2025:2024-05-03:2025-05-03,oos_2025_2026:2025-05-03:2026-05-03",
    [string]$SignalTimes = "22:15",
    [string]$EntryDelays = "1,15,60",
    [string]$Horizons = "60,180",
    [string]$Scores = "vol_adjusted_day_return,day_return,late_volume_ratio,vwap_extension,pump_score",
    [string]$TopNs = "3,5,10",
    [int]$Buckets = 10,
    [int]$MinObsPerBucket = 20,
    [string]$PumpFilter = "pump",
    [int]$LiquidityRankMin = 31,
    [int]$LiquidityRankMax = 150,
    [double]$CostMultiplier = 1.0
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = Join-Path $RepoRoot ".venv/bin/python"
}
if (-not (Test-Path $Python)) {
    throw "Virtualenv not found. Run scripts/windows_setup.ps1 on Windows or create .venv on macOS/Linux first."
}

& $Python .\scripts\run_daily_close_fade_split_diagnostics.py `
    --data-root $DataRoot `
    --config $Config `
    --splits $Splits `
    --signal-times $SignalTimes `
    --entry-delays $EntryDelays `
    --horizons $Horizons `
    --scores $Scores `
    --top-ns $TopNs `
    --buckets $Buckets `
    --min-obs-per-bucket $MinObsPerBucket `
    --pump-filter $PumpFilter `
    --liquidity-rank-min $LiquidityRankMin `
    --liquidity-rank-max $LiquidityRankMax `
    --cost-multiplier $CostMultiplier

if ($LASTEXITCODE -ne 0) {
    throw "Daily close fade split diagnostics failed with exit code $LASTEXITCODE."
}
