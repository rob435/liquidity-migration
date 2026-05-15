param(
    [string]$Remote = "https://github.com/rob435/MODEL05042026.git",
    [string]$Repo = "",
    [string]$DataRoot = "",
    [string]$ConfigPath = "configs/volume_alpha.default.yaml",
    [string]$RunName = "fullpit-1h-all-usdt-20230503-20260503",
    [string]$ManifestName = "pit-all-usdt-20230503-20260503",
    [string]$StartDate = "2023-05-03",
    [string]$EndDate = "2026-05-03",
    [int]$ManifestWorkers = 32,
    [int]$DownloadWorkers = 64,
    [int]$MinExistingBars = 20,
    [string]$Python = "python",
    [bool]$RunTests = $true,
    [bool]$RunChampionBacktest = $true,
    [bool]$RunEventGrid = $true,
    [double]$ChampionGrossExposure = 0.5,
    [string]$EventGridEventTypes = "fresh_volume_spike,persistent_volume_breakout,tail_liquidity_jump,volume_exhaustion",
    [string]$EventGridThresholds = "0.2,0.3",
    [string]$EventGridHoldDays = "3,5,7,14",
    [string]$EventGridSides = "continuation,reversal",
    [string]$EventGridStopLossPcts = "0,0.03,0.05,0.08,0.12",
    [string]$EventGridCostMultipliers = "1,3",
    [double]$EventGridGrossExposure = 0.5,
    [string]$EventGridMaxActiveList = "6,12",
    [string]$EventGridCooldownList = "3,7",
    [string]$EventGridEntryDelayList = "1,6,12",
    [string]$EventGridRankExitThresholds = "0.5",
    [int]$EventGridUniverseRankMin = 1,
    [int]$EventGridUniverseRankMax = 0,
    [double]$EventGridUniverseMinDailyTurnover = 0.0,
    [int]$EventGridTailRankMin = 81,
    [int]$EventGridTailRankMax = 160,
    [int]$EventGridTailRankImprovementMin = 20,
    [double]$EventGridExhaustionMinDayReturn = 0.03
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

function Split-Csv {
    param([string]$Value)
    return @($Value.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" })
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
    $DataRoot = Join-Path $HOME "agc-bybit-fullpit-1h-20230503-20260503"
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
            "tests/test_aggression_carry_cli.py::test_cli_parses_volume_events",
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

    Section "Download full PIT 1h klines"
    if (-not $env:AGC_ARCHIVE_DOWNLOAD_BACKEND) {
        $env:AGC_ARCHIVE_DOWNLOAD_BACKEND = "curl"
    }
    if (-not $env:AGC_ARCHIVE_DOWNLOAD_RETRIES) {
        $env:AGC_ARCHIVE_DOWNLOAD_RETRIES = "8"
    }
    Invoke-Checked $VenvPython @(
        "-m", "aggression_carry",
        "--data-root", $DataRoot,
        "--config", $ConfigPath,
        "archive-download-klines-1h",
        "--name", $RunName,
        "--start", $StartDate,
        "--end", $EndDate,
        "--workers", "$DownloadWorkers",
        "--min-existing-bars", "$MinExistingBars",
        "--discard-archives-after-success"
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
report_path = root / "reports" / f"archive_klines_1h_{run_name}.csv"

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
    Set-Content -Path $EventReportIndex -Value "run_type,max_active_symbols,cooldown_days,entry_delay_hours,rank_exit_threshold,event_types,thresholds,hold_days,sides,stop_loss_pcts,cost_multipliers,gross_exposure,report_dir" -Encoding UTF8

    if ($RunChampionBacktest) {
        Section "Run selected full PIT volume event backtest"
        $ChampionReportDir = Join-Path (Join-Path $DataRoot "reports") ("volume_event_research_fullpit_pvb_q20_cont_h5_halfgross_{0}" -f ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")))
        Invoke-Checked $VenvPython @(
            "-m", "aggression_carry",
            "--data-root", $DataRoot,
            "--config", $ConfigPath,
            "volume-events",
            "--event-types", "persistent_volume_breakout",
            "--thresholds", "0.2",
            "--hold-days", "5",
            "--sides", "continuation",
            "--stop-loss-pcts", "0",
            "--cost-multipliers", "1,3",
            "--gross-exposure", "$ChampionGrossExposure",
            "--entry-delay-hours", "1",
            "--max-active-symbols", "6",
            "--cooldown-days", "7",
            "--rank-exit-threshold", "0.5",
            "--report-dir", $ChampionReportDir
        )
        Add-Content -Path $EventReportIndex -Value "champion,6,7,1,0.5,persistent_volume_breakout,0.2,5,continuation,0,1|3,$ChampionGrossExposure,$ChampionReportDir"
    }

    if ($RunEventGrid) {
        Section "Run full PIT event-driven feature grid"
        foreach ($MaxActive in (Split-Csv $EventGridMaxActiveList)) {
            foreach ($Cooldown in (Split-Csv $EventGridCooldownList)) {
                foreach ($EntryDelay in (Split-Csv $EventGridEntryDelayList)) {
                    foreach ($RankExitThreshold in (Split-Csv $EventGridRankExitThresholds)) {
                        $RankTag = $RankExitThreshold.Replace(".", "p")
                        $GridReportDir = Join-Path (Join-Path $DataRoot "reports") ("volume_event_research_fullpit_grid_ma{0}_cd{1}_ed{2}_rx{3}_{4}" -f $MaxActive, $Cooldown, $EntryDelay, $RankTag, ([DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")))
                        Write-Host "Starting event grid: max_active=$MaxActive cooldown=$Cooldown entry_delay=$EntryDelay rank_exit=$RankExitThreshold report=$GridReportDir"
                        Invoke-Checked $VenvPython @(
                            "-m", "aggression_carry",
                            "--data-root", $DataRoot,
                            "--config", $ConfigPath,
                            "volume-events",
                            "--event-types", $EventGridEventTypes,
                            "--thresholds", $EventGridThresholds,
                            "--hold-days", $EventGridHoldDays,
                            "--sides", $EventGridSides,
                            "--stop-loss-pcts", $EventGridStopLossPcts,
                            "--cost-multipliers", $EventGridCostMultipliers,
                            "--gross-exposure", "$EventGridGrossExposure",
                            "--entry-delay-hours", "$EntryDelay",
                            "--max-active-symbols", "$MaxActive",
                            "--cooldown-days", "$Cooldown",
                            "--rank-exit-threshold", "$RankExitThreshold",
                            "--universe-rank-min", "$EventGridUniverseRankMin",
                            "--universe-rank-max", "$EventGridUniverseRankMax",
                            "--universe-min-daily-turnover", "$EventGridUniverseMinDailyTurnover",
                            "--tail-rank-min", "$EventGridTailRankMin",
                            "--tail-rank-max", "$EventGridTailRankMax",
                            "--tail-rank-improvement-min", "$EventGridTailRankImprovementMin",
                            "--exhaustion-min-day-return", "$EventGridExhaustionMinDayReturn",
                            "--report-dir", $GridReportDir
                        )
                        Add-Content -Path $EventReportIndex -Value ("event_grid,{0},{1},{2},{3},{4},{5},{6},{7},{8},{9},{10},{11}" -f `
                            $MaxActive, `
                            $Cooldown, `
                            $EntryDelay, `
                            $RankExitThreshold, `
                            $EventGridEventTypes.Replace(",", "|"), `
                            $EventGridThresholds.Replace(",", "|"), `
                            $EventGridHoldDays.Replace(",", "|"), `
                            $EventGridSides.Replace(",", "|"), `
                            $EventGridStopLossPcts.Replace(",", "|"), `
                            $EventGridCostMultipliers.Replace(",", "|"), `
                            $EventGridGrossExposure, `
                            $GridReportDir)
                    }
                }
            }
        }
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
