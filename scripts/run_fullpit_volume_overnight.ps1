param(
    [string]$Remote = "https://github.com/rob435/MODEL05042026.git",
    [string]$Repo = "",
    [string]$DataRoot = "",
    [string]$ConfigPath = "configs/volume_alpha.default.yaml",
    [string]$RunName = "canonical-fullpit-1h-all-usdt-20230503-20260518",
    [string]$ManifestName = "canonical-pit-all-usdt-20230503-20260518",
    [string]$StartDate = "2023-05-03",
    [string]$EndDate = "2026-05-18",
    [int]$ManifestWorkers = 32,
    [int]$DownloadWorkers = 16,
    [int]$MinExistingBars = 1,
    [string]$Python = "python",
    [bool]$RunTests = $true,
    [bool]$RunChampionBacktest = $true,
    [double]$ChampionGrossExposure = 1.0
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Section {
    param([string]$Name)
    Write-Host ""
    Write-Host "== $Name =="
    Write-Host ([DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ"))
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

if ($Repo -eq "") {
    if ($PSScriptRoot -ne "") {
        $Repo = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    }
    else {
        $Repo = Join-Path $HOME "MODEL050426"
    }
}
if ($DataRoot -eq "") {
    $DataRoot = Join-Path (Join-Path $HOME "SHARED_DATA") "bybit_fullpit_1h"
}

$LogDir = Join-Path $DataRoot "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("fullpit_volume_overnight_{0}.log" -f ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")))
Start-Transcript -Path $LogFile -Append | Out-Null

try {
    Section "Sync repo"
    if (-not (Test-Path (Join-Path $Repo ".git"))) {
        Invoke-Checked "git" @("clone", $Remote, $Repo)
    }

    Set-Location $Repo
    Invoke-Checked "git" @("update-index", "-q", "--refresh")
    & git diff-index --quiet HEAD --
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Tracked local changes exist in $Repo; refusing to overwrite them."
        git status --short
        exit 2
    }

    Invoke-Checked "git" @("fetch", "origin", "main", "--prune")
    Invoke-Checked "git" @("switch", "main")
    Invoke-Checked "git" @("pull", "--ff-only", "origin", "main")
    Write-Host "Repo: $Repo"
    Write-Host "Commit: $(git rev-parse --short HEAD)"
    git log --oneline -3

    Section "Install runtime"
    if (-not (Test-Path ".venv")) {
        Invoke-Checked $Python @("-m", "venv", ".venv")
    }
    $VenvPython = Join-Path $Repo ".venv\Scripts\python.exe"
    if (-not (Test-Path $VenvPython)) {
        $VenvPython = Join-Path $Repo ".venv/bin/python"
    }
    if (-not (Test-Path $VenvPython)) {
        throw "Could not find venv Python under .venv\Scripts or .venv/bin"
    }

    Invoke-Checked $VenvPython @("-m", "pip", "install", "-U", "pip")
    Invoke-Checked $VenvPython @("-m", "pip", "install", "-e", ".[dev]")

    if ($RunTests) {
        Section "Smoke tests"
        Invoke-Checked $VenvPython @(
            "-m", "pytest",
            "tests/test_aggression_carry_cli.py::test_cli_parses_volume_events_research_overrides",
            "tests/test_aggression_carry_archive.py::test_archive_hourly_kline_download_writes_1h_partitions",
            "tests/test_aggression_carry_archive.py::test_archive_hourly_downloader_processes_each_symbol_in_date_order",
            "tests/test_aggression_carry_volume_events.py"
        )
    }

    Section "Build full PIT manifest"
    Invoke-Checked $VenvPython @(
        "-m", "aggression_carry",
        "--data-root", $DataRoot,
        "--config", $ConfigPath,
        "archive-manifest",
        "--name", $ManifestName,
        "--start", $StartDate,
        "--end", $EndDate,
        "--workers", "$ManifestWorkers"
    )

    Section "Fill full PIT 1h klines from Bybit v5 API"
    Invoke-Checked $VenvPython @(
        "-m", "aggression_carry",
        "--data-root", $DataRoot,
        "--config", $ConfigPath,
        "archive-download-klines-1h-api",
        "--name", $RunName,
        "--start", $StartDate,
        "--end", $EndDate,
        "--workers", "$DownloadWorkers",
        "--min-existing-bars", "$MinExistingBars",
        "--limit", "1000",
        "--retries", "8",
        "--timeout-seconds", "30",
        "--request-sleep-seconds", "0.02"
    )

    Section "Validate full PIT coverage"
    $env:DATA_ROOT = $DataRoot
    $env:RUN_NAME = $RunName
    $ValidationCode = @'
import csv
import os
import sys
from collections import Counter
from pathlib import Path

from pyarrow import parquet as pq

from aggression_carry.storage import dataset_path, read_dataset

root = Path(os.environ["DATA_ROOT"])
run_name = os.environ["RUN_NAME"]
report_path = root / "reports" / f"archive_klines_1h_api_{run_name}.csv"

if not report_path.exists():
    raise SystemExit(f"missing downloader report: {report_path}")

with report_path.open(newline="") as handle:
    rows = list(csv.DictReader(handle))

status = Counter(row["status"] for row in rows)
failures = [row for row in rows if row["status"] == "failed" or row.get("error")]
print({"download_report_rows": len(rows), "status": dict(status), "failures": len(failures)})
if failures:
    print("failure_sample", failures[:20])
    sys.exit(1)

manifest = read_dataset(root, "archive_trade_manifest").select(["symbol", "date"]).unique()
base = dataset_path(root, "klines_1h")
missing = []
thin = []
for row in manifest.to_dicts():
    part = base / f"date={row['date']}" / f"symbol={row['symbol']}" / "part.parquet"
    if not part.exists():
        missing.append(row)
        continue
    count = pq.ParquetFile(part).metadata.num_rows
    if count < 20:
        thin.append({**row, "rows": count})

print({"manifest_rows": manifest.height, "missing_partitions": len(missing), "thin_partitions": len(thin)})
if missing:
    print("missing_sample", missing[:20])
    sys.exit(1)
'@
    $ValidationFile = Join-Path $env:TEMP "agc_validate_fullpit.py"
    Set-Content -Path $ValidationFile -Value $ValidationCode -Encoding UTF8
    Invoke-Checked $VenvPython @($ValidationFile)

    $EventReportIndex = Join-Path (Join-Path $DataRoot "reports") ("fullpit_volume_event_runs_{0}.csv" -f ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")))
    Set-Content -Path $EventReportIndex -Value "run_type,max_active_symbols,cooldown_days,entry_delay_hours,entry_policy,rank_exit_threshold,universe_rank_min,universe_rank_max,liquidity_migration_rank_improvement_min,liquidity_migration_turnover_ratio_min,liquidity_migration_event_rank_fraction_max,liquidity_migration_day_return_min,liquidity_migration_residual_return_min,liquidity_migration_market_pct_up_max,liquidity_migration_hot_market_day_return_min,liquidity_migration_hot_market_day_return_band,liquidity_migration_close_location_min,liquidity_migration_pit_age_days_min,liquidity_migration_crowding_filter,stop_pressure_window_days,stop_pressure_stop_count,realized_loss_pressure_window_days,realized_loss_pressure_loss_count,event_types,thresholds,hold_days,sides,stop_loss_pcts,take_profit_pcts,cost_multipliers,gross_exposure,report_dir" -Encoding UTF8

    if ($RunChampionBacktest) {
        Section "Run selected full PIT volume event backtest"
        $ChampionReportDir = Join-Path (Join-Path $DataRoot "reports") ("SELECTED_liqmig_union_q40_h3_tp26_g100_qsqueeze_{0}" -f ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")))
        Invoke-Checked $VenvPython @(
            "-m", "aggression_carry",
            "--data-root", $DataRoot,
            "--config", $ConfigPath,
            "volume-events",
            "--event-types", "liquidity_migration",
            "--thresholds", "0.4",
            "--hold-days", "3",
            "--sides", "reversal",
            "--stop-loss-pcts", "0.12",
            "--take-profit-pcts", "0.26",
            "--cost-multipliers", "3",
            "--gross-exposure", "$ChampionGrossExposure",
            "--entry-delay-hours", "1",
            "--entry-policy", "promoted_quality_squeeze",
            "--max-active-symbols", "5",
            "--cooldown-days", "5",
            "--rank-exit-threshold", "0.55",
            "--universe-rank-min", "31",
            "--universe-rank-max", "150",
            "--liquidity-migration-rank-improvement-min", "150",
            "--liquidity-migration-turnover-ratio-min", "6.0",
            "--liquidity-migration-event-rank-fraction-max", "0.90",
            "--liquidity-migration-event-rank-fraction-exclude-min", "0",
            "--liquidity-migration-event-rank-fraction-exclude-max", "0",
            "--liquidity-migration-day-return-min", "0.0",
            "--liquidity-migration-residual-return-min", "0.08",
            "--liquidity-migration-market-pct-up-max", "0.65",
            "--liquidity-migration-hot-market-day-return-min", "0.16",
            "--liquidity-migration-hot-market-day-return-band", "0.015",
            "--liquidity-migration-close-location-min", "0.45",
            "--liquidity-migration-pit-age-days-min", "90",
            "--liquidity-migration-crowding-filter", "union_pathology",
            "--stop-pressure-window-days", "10",
            "--stop-pressure-stop-count", "7",
            "--realized-loss-pressure-window-days", "5",
            "--realized-loss-pressure-loss-count", "6",
            "--realized-loss-pressure-min-loss-abs", "0.0",
            "--report-dir", $ChampionReportDir
        )
        Add-Content -Path $EventReportIndex -Value "champion,5,5,1,promoted_quality_squeeze,0.55,31,150,150,6.0,0.90,0.0,0.08,0.65,0.16,0.015,0.45,90,union_pathology,10,7,5,6,liquidity_migration,0.4,3,reversal,0.12,0.26,3,$ChampionGrossExposure,$ChampionReportDir"
    }

    Section "Done"
    Write-Host "Log: $LogFile"
    Write-Host "Data root: $DataRoot"
    Write-Host "Event report index: $EventReportIndex"
    Get-Content $EventReportIndex
}
finally {
    Stop-Transcript | Out-Null
}
