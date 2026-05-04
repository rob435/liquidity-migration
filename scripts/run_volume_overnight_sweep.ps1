param(
    [ValidateSet("deep", "tail", "insane")]
    [string]$Preset = "deep",
    [string]$DataRoot = "data/agc-bybit-3y-auto150-20230503-20260503",
    [string]$UniverseRoot = "data/universe-research",
    [string]$UniverseName = "top160-current",
    [int]$RankEnd = 160,
    [int]$MaxSymbols = 160,
    [string]$Start = "2023-05-03",
    [string]$End = "2026-05-03",
    [int]$Workers = 8,
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

function Get-PresetConfig {
    param([string]$Name)

    if ($Name -eq "tail") {
        return @{
            Buckets = "tail_a:81-120,tail_b:121-160,tail_all:81-160,mid_tail:51-160"
            Quantiles = "0.1,0.15,0.2,0.25,0.3,0.4,0.5"
            HoldDays = "1,2,3,5,7,10,14,21,28"
            FixedStops = "0,0.08,0.12,0.2,0.3"
            VolStops = "2,2.5,3,4,5"
            RankExits = "false,true"
            CostMultipliers = "1"
            TakeProfits = "0"
        }
    }

    if ($Name -eq "insane") {
        return @{
            Buckets = "core:1-20,upper_mid:21-50,lower_mid:51-80,tail_a:81-120,tail_b:121-160,tail_all:81-160,mid_tail:51-160,broad:1-160"
            Quantiles = "0.1,0.15,0.2,0.25,0.3,0.4,0.5"
            HoldDays = "1,2,3,5,7,10,14,21,28"
            FixedStops = "0,0.08,0.12,0.2,0.3"
            VolStops = "2,2.5,3,4,5"
            RankExits = "false,true"
            CostMultipliers = "1"
            TakeProfits = "0"
        }
    }

    return @{
        Buckets = "core:1-20,mid:21-80,tail:81-160,broad:1-160"
        Quantiles = "0.1,0.15,0.2,0.3,0.5"
        HoldDays = "1,2,3,5,7,10,14,21"
        FixedStops = "0,0.12,0.2,0.3"
        VolStops = "2.5,3,4"
        RankExits = "false,true"
        CostMultipliers = "1"
        TakeProfits = "0"
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
$LogPath = Join-Path $LogDir "overnight_sweep_$Preset`_$Stamp.log"
$Grid = Get-PresetConfig $Preset

Start-Transcript -Path $LogPath -Append | Out-Null
try {
    Write-Host "Repo: $RepoRoot"
    Write-Host "Preset: $Preset"
    Write-Host "Data root: $DataRoot"
    Write-Host "Log: $LogPath"
    Write-Host "Workers: $Workers"
    Write-Host "Buckets: $($Grid.Buckets)"
    Write-Host "Quantiles: $($Grid.Quantiles)"
    Write-Host "Hold days: $($Grid.HoldDays)"
    Write-Host "Fixed stops: $($Grid.FixedStops)"
    Write-Host "Vol stops: $($Grid.VolStops)"

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
            --min-age-days 30 `
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
        Invoke-Checked "Downloading resumable 3-year Bybit klines" {
            & $Python -m aggression_carry `
                --data-root $DataRoot `
                --config $Config `
                download-data `
                --symbols $SymbolCsv `
                --start $Start `
                --end $End `
                --datasets "instruments,klines_1h"
        }
    }
    else {
        Write-Host "Skipping download because -SkipDownload was provided."
    }

    Invoke-Checked "Writing volume-alpha feature report" {
        & $Python -m aggression_carry `
            --data-root $DataRoot `
            --config $Config `
            volume-alpha
    }

    Invoke-Checked "Running overnight bucket/grid sweep" {
        & $Python .\scripts\run_volume_bucket_sweep.py `
            --data-root $DataRoot `
            --config $Config `
            --workers $Workers `
            --buckets $Grid.Buckets `
            --quantiles $Grid.Quantiles `
            --hold-days $Grid.HoldDays `
            --fixed-stops $Grid.FixedStops `
            --vol-stops $Grid.VolStops `
            --rank-exits $Grid.RankExits `
            --take-profits $Grid.TakeProfits `
            --cost-multipliers $Grid.CostMultipliers `
            --include-reverse
    }

    Write-Host ""
    Write-Host "Done."
    Write-Host "Summary: $DataRoot/reports/volume_bucket_sweep_summary.md"
    Write-Host "CSV: $DataRoot/reports/volume_bucket_sweep_summary.csv"
    Write-Host "Log: $LogPath"
}
finally {
    Stop-Transcript | Out-Null
}
