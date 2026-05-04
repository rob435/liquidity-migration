param(
    [ValidateSet("both", "daily-close", "volume")]
    [string]$Suite = "both",
    [string]$Config = "configs/volume_alpha.default.yaml",
    [int]$Workers = 8,
    [switch]$SkipGitPull,
    [switch]$SkipSetup,
    [switch]$SkipVolumeDownload,
    [switch]$RequireDailyCloseData,
    [string]$DailyCloseDataRoot = "data/daily-close-fade-1m-3y-current-top160-20230503-20260503",
    [string]$DailyCloseReportDir = "data/research_reports/risk_on_breadth_sizing_5950x",
    [string]$DailyCloseFilters = "8:0.035:1.0:1,8:0.035:1.0:2,8:0.035:1.0:3,8:0.035:1.0:4,5:0.025:0.75:3,5:0.025:0.75:4,5:0.025:0.75:5",
    [string]$DailyCloseMaxWeights = "0.25,0.30,0.35,0.40,0.50,0.80",
    [string]$DailyCloseScorePowers = "0.5,1.0",
    [ValidateSet("promotion", "deep", "tail", "insane")]
    [string]$VolumePreset = "promotion",
    [string]$VolumeDataRoot = "data/agc-bybit-3y-auto150-20230503-20260503",
    [string]$VolumeUniverseRoot = "data/universe-research",
    [string]$VolumeUniverseName = "top160-current",
    [string]$VolumeScores = "",
    [string]$VolumeSplitBuckets = "",
    [switch]$SkipVolumeSplitPromotion,
    [switch]$SkipResearchLog
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

function Invoke-AllowGateFailure {
    param(
        [string]$Name,
        [scriptblock]$Command
    )

    Write-Host ""
    Write-Host "==> $Name"
    & $Command
    if ($LASTEXITCODE -eq 2) {
        Write-Warning "$Name completed with failing or missing gates. Continuing so the run can be logged."
        return
    }
    if ($LASTEXITCODE -ne 0) {
        throw "$Name failed with exit code $LASTEXITCODE."
    }
}

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = Join-Path $RepoRoot ".venv/bin/python"
}

$LogRoot = Join-Path $RepoRoot "data/research_reports/logs"
New-Item -ItemType Directory -Force -Path $LogRoot | Out-Null
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $LogRoot "research_overnight_suite_$Stamp.log"

Start-Transcript -Path $LogPath -Append | Out-Null
try {
    Write-Host "Repo: $RepoRoot"
    Write-Host "Suite: $Suite"
    Write-Host "Workers: $Workers"
    Write-Host "Config: $Config"
    Write-Host "Log: $LogPath"

    if (-not $SkipGitPull) {
        if (Test-Path (Join-Path $RepoRoot ".git")) {
            if (-not (Test-CommandExists "git")) {
                throw "git is not on PATH. Install Git for Windows, reopen PowerShell, then rerun this script."
            }
            Invoke-Checked "Pulling latest main with autostash" {
                & git fetch origin
                if ($LASTEXITCODE -ne 0) { throw "git fetch failed with exit code $LASTEXITCODE." }
                & git checkout main
                if ($LASTEXITCODE -ne 0) { throw "git checkout main failed with exit code $LASTEXITCODE." }
                & git branch --set-upstream-to=origin/main main
                if ($LASTEXITCODE -ne 0) { throw "git branch upstream update failed with exit code $LASTEXITCODE." }
                & git pull --rebase --autostash
            }
        }
        else {
            Write-Host "Skipping git pull because this folder is not a git clone."
        }
    }
    else {
        Write-Host "Skipping git pull because -SkipGitPull was provided."
    }

    if (-not $SkipSetup) {
        Invoke-Checked "Installing/updating Windows Python environment" {
            & powershell -ExecutionPolicy Bypass -File .\scripts\windows_setup.ps1
        }
    }
    else {
        Write-Host "Skipping setup because -SkipSetup was provided."
    }

    $Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $Python)) {
        $Python = Join-Path $RepoRoot ".venv/bin/python"
    }
    if (-not (Test-Path $Python)) {
        throw "Virtualenv not found after setup. Run .\scripts\windows_setup.ps1 first."
    }

    Invoke-Checked "Checking virtualenv Python version" {
        & $Python -c "import sys; print(sys.executable); print(sys.version); raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
    }

    $ShouldRunDailyClose = ($Suite -eq "both" -or $Suite -eq "daily-close")
    $DailyCloseWasRun = $false

    if ($ShouldRunDailyClose) {
        if (-not (Test-Path (Join-Path $RepoRoot $DailyCloseDataRoot))) {
            $DailyCloseMissingMessage = "Daily-close data root not found: $DailyCloseDataRoot. This is a separate 1m kline dataset and is not downloaded by the volume sweep."
            if ($Suite -eq "daily-close" -or $RequireDailyCloseData) {
                throw "$DailyCloseMissingMessage Copy/download the 1m data, pass -DailyCloseDataRoot, or run -Suite volume."
            }
            Write-Warning "$DailyCloseMissingMessage Skipping daily-close research and continuing with the volume suite."
        }
        else {
            Invoke-Checked "Running daily-close fade breadth/sizing research" {
                & $Python .\scripts\run_daily_close_fade_sizing_sweep.py `
                    --data-root $DailyCloseDataRoot `
                    --config $Config `
                    --filters $DailyCloseFilters `
                    --max-weights $DailyCloseMaxWeights `
                    --score-powers $DailyCloseScorePowers `
                    --include-uncapped `
                    --report-dir $DailyCloseReportDir
            }
            $DailyCloseWasRun = $true
        }
    }

    if ($Suite -eq "both" -or $Suite -eq "volume") {
        Invoke-Checked "Running volume-alpha overnight sweep" {
            & .\scripts\run_volume_overnight_sweep.ps1 `
                -Preset $VolumePreset `
                -DataRoot $VolumeDataRoot `
                -UniverseRoot $VolumeUniverseRoot `
                -UniverseName $VolumeUniverseName `
                -Workers $Workers `
                -Scores $VolumeScores `
                -SplitBuckets $VolumeSplitBuckets `
                -SkipDownload:$SkipVolumeDownload `
                -SkipSplitPromotion:$SkipVolumeSplitPromotion `
                -NoTranscript
        }
    }

    if (-not $SkipResearchLog) {
        Invoke-AllowGateFailure "Writing research readiness report" {
            & $Python .\scripts\report_research_readiness.py
        }

        $ArtifactArgs = @(
            ".\scripts\write_research_run_record.py",
            "--name", "overnight-research-suite",
            "--strategy", $Suite,
            "--status", "benchmark",
            "--bias", "current_universe_biased",
            "--intent", "Automated overnight research sweep with explicit artifacts, gates, and hashes.",
            "--decision", "Do not promote from headline return alone; inspect split gates and artifact manifests.",
            "--next-step", "Review research_log.md, promotion reports, and any failing gates before changing config.",
            "--constraint", "Use 8 workers on Windows to avoid Python process-spawn and memory failures.",
            "--constraint", "Current-top universe results remain biased until point-in-time archive validation exists.",
            "--tag", "overnight",
            "--config", $Config,
            "--artifact", "data/research_reports/readiness/research_readiness_report.json",
            "--artifact", "data/research_reports/readiness/research_readiness_report.md"
        )
        if ($Suite -eq "both" -or $Suite -eq "daily-close") {
            $ArtifactArgs += @(
                "--data-root", $DailyCloseDataRoot,
                "--artifact", "$DailyCloseReportDir/daily_close_fade_sizing_sweep.md",
                "--artifact", "$DailyCloseReportDir/daily_close_fade_sizing_sweep.csv"
            )
        }
        if ($Suite -eq "both" -or $Suite -eq "volume") {
            $ArtifactArgs += @(
                "--data-root", $VolumeDataRoot,
                "--artifact", "$VolumeDataRoot/reports/volume_alpha_report.json",
                "--artifact", "$VolumeDataRoot/reports/volume_alpha_report.md",
                "--artifact", "$VolumeDataRoot/reports/volume_bucket_sweep_summary.csv",
                "--artifact", "$VolumeDataRoot/reports/volume_bucket_sweep_summary.md",
                "--artifact-glob", "$VolumeDataRoot/reports/volume_promotion_splits/*/volume_grid_split_summary.csv",
                "--artifact-glob", "$VolumeDataRoot/reports/volume_promotion_splits/*/volume_grid_split_summary.md",
                "--artifact-glob", "$VolumeDataRoot/reports/volume_promotion_splits/*/promotion/volume_promotion_report.json",
                "--artifact-glob", "$VolumeDataRoot/reports/volume_promotion_splits/*/promotion/volume_promotion_report.md",
                "--artifact-glob", "$VolumeDataRoot/reports/volume_promotion_splits/*/promotion/volume_promotion_candidates.csv"
            )
        }

        Invoke-Checked "Writing research run record" {
            & $Python @ArtifactArgs
        }
    }
    else {
        Write-Host "Skipping research log because -SkipResearchLog was provided."
    }

    Write-Host ""
    Write-Host "Done."
    if ($DailyCloseWasRun) {
        Write-Host "Daily-close report: $DailyCloseReportDir/daily_close_fade_sizing_sweep.md"
    }
    elseif ($ShouldRunDailyClose) {
        Write-Host "Daily-close report: skipped because $DailyCloseDataRoot was not found."
    }
    if ($Suite -eq "both" -or $Suite -eq "volume") {
        Write-Host "Volume report: $VolumeDataRoot/reports/volume_bucket_sweep_summary.md"
    }
    if (-not $SkipResearchLog) {
        Write-Host "Research log: data/research_reports/research_log/research_log.md"
    }
    Write-Host "Log: $LogPath"
}
finally {
    Stop-Transcript | Out-Null
}
