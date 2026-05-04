# Windows Fresh Clone Quickstart

Use this when setting up a fresh Windows PC from zero.

## 1. Install Git And Python

Open PowerShell as your normal user, not Administrator.

```powershell
winget install -e --id Git.Git
winget install -e --id Python.Python.3.11
```

If `winget` or Microsoft Store is unavailable, install Git from
`https://git-scm.com/download/win` and Python 3.11 or newer from
`https://www.python.org/downloads/windows/`. During Python install, keep the
Python Launcher option enabled.

Close PowerShell completely, then open a new PowerShell window.

```powershell
git --version
py -0p
py -3 --version
```

## 2. Clone The Repo

```powershell
cd $HOME\Desktop
git clone https://github.com/rob435/MODEL05042026.git
cd MODEL05042026
git switch main
git reset --hard origin/main
```

## 3. Create The Virtualenv

Preferred:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows_setup.ps1
```

Manual equivalent:

```powershell
py -3 -m venv --clear .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
```

If `.\.venv\Scripts\python.exe` is not found:

```powershell
Remove-Item -Recurse -Force .venv -ErrorAction SilentlyContinue
py -3 -m venv .venv
```

Then rerun the install commands.

## 4. Run A Tiny Fixture Check

This does not download Bybit data. It proves the local volume-alpha path works.

```powershell
.\.venv\Scripts\python.exe -m aggression_carry --data-root .tmp/volume-fixture download-data --fixture
.\.venv\Scripts\python.exe -m aggression_carry --data-root .tmp/volume-fixture volume-alpha
.\.venv\Scripts\python.exe -m aggression_carry --data-root .tmp/volume-fixture volume-backtest --hold-days 1 --rebalance-days 1
.\.venv\Scripts\python.exe -m aggression_carry --data-root .tmp/volume-fixture volume-grid --hold-days 1 --quantiles 0.5 --fixed-stops "0,0.001" --vol-stops "" --rank-exits "false,true" --workers 2
```

Report:

```text
.tmp/volume-fixture/reports/volume_alpha_report.md
.tmp/volume-fixture/reports/volume_backtest_report.md
.tmp/volume-fixture/reports/volume_grid_report.md
```

## 5. Run The Current Overnight Research Suite

This is the current preferred Windows path. It runs git pull, setup, the
daily-close breadth/sizing research when the separate 1m data root exists, then
the volume-alpha promotion sweep. Use 8 workers on the 5950X; higher worker
counts have caused Windows/Python memory and process-spawn failures on
multi-year grids.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_research_overnight_suite.ps1 `
  -Suite both `
  -Workers 8
```

Reports:

```text
data/research_reports/risk_on_breadth_sizing_5950x/daily_close_fade_sizing_sweep.md
data/agc-bybit-3y-auto150-20230503-20260503/reports/volume_bucket_sweep_summary.md
data/agc-bybit-3y-auto150-20230503-20260503/reports/volume_promotion_splits/<bucket>/volume_grid_split_summary.md
data/agc-bybit-3y-auto150-20230503-20260503/reports/volume_promotion_splits/<bucket>/promotion/volume_promotion_report.md
data/research_reports/research_log/research_log.md
```

The volume leg defaults to `-VolumePreset promotion`. That preset tests
`dollar_volume_rank`, `volume_change_1d`, `volume_change_3d`,
`volume_persistence`, and `volume_composite`, then applies fixed train,
validation, and OOS promotion gates by liquidity bucket.
It also writes a research run record with git metadata, data roots, config
hashes, artifact hashes, bias labels, and promotion-gate summaries.

The daily-close data root is large 1m kline data and is not part of the volume
download. If it is missing, `-Suite both` warns, skips daily-close, and continues
the volume sweep. Use `-Suite daily-close` when you specifically want that side
to fail unless the 1m data is present.

If the 3-year volume data is already downloaded and you only want to rerun the
grids:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_research_overnight_suite.ps1 `
  -Suite both `
  -Workers 8 `
  -SkipVolumeDownload
```

Run only one side:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_research_overnight_suite.ps1 -Suite daily-close -Workers 8
powershell -ExecutionPolicy Bypass -File .\scripts\run_research_overnight_suite.ps1 -Suite volume -Workers 8
```

Large Bybit downloads are resumable. The downloader prints one line per
symbol/dataset and writes completed chunks immediately. If PowerShell is stopped
with Ctrl+C or a network timeout kills the command, rerun the same command; rows
already completed will show as `cached`.

## Notes

- The current official paths are isolated research systems: volume rank and
  daily-close fade. Do not blend them until each clears costs standalone.
- `volume-alpha` is the statistical sweep. `volume-backtest` is the trade ledger
  with entries, exits, exit reasons, stops, costs, and symbol/month attribution.
- `daily-close-fade-grid` tests the 1m UTC close-window short fade with top-N,
  pump-tag, stop, trailing-stop, hold-time, and cost sensitivity.
- `volume-grid` and `daily-close-fade-grid` run parameter variants
  concurrently. On Windows they use thread workers to avoid `spawn` trying to
  pickle multi-year Polars datasets into every child process. On macOS/Linux
  they still use process workers.
- If a large 3-year sweep fails with pickle, memory allocation, or worker spawn
  errors, pull the latest code and rerun the same command. The data cache should
  prevent redownloading completed market data. If RAM pressure is still high,
  rerun with `--workers 8`, then `--workers 4`, then `--workers 1` as the
  conservative fallback.
- The RTX GPU is not used by this path. The current bottleneck is Python trade
  simulation across independent variants. CPU concurrency is the correct
  optimization before any CUDA rewrite.
- The old `build-features`, `alpha-report`, `portfolio-backtest`, and
  `research-sweep` commands were removed with the old composite stack.
- This is a research backtest path only. It does not place live orders.
- If Bybit REST rejects your network or region, the download step will fail. Run
  from a network where Bybit public market endpoints are reachable.
