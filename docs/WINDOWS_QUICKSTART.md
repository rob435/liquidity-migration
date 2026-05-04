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

## 5. Run The 3-Month Bybit Test

Preferred:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_agc_3m.ps1
```

Manual equivalent:

```powershell
.\.venv\Scripts\python.exe -m aggression_carry --data-root data/agc-bybit-3m --config configs/volume_alpha.default.yaml download-data --symbols "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,LINKUSDT,AVAXUSDT,APTUSDT,BNBUSDT,ADAUSDT,DOTUSDT,LTCUSDT,NEARUSDT,OPUSDT,ARBUSDT,INJUSDT" --start "2025-01-01" --end "2025-04-01" --datasets "instruments,klines_1h"
.\.venv\Scripts\python.exe -m aggression_carry --data-root data/agc-bybit-3m --config configs/volume_alpha.default.yaml volume-alpha
.\.venv\Scripts\python.exe -m aggression_carry --data-root data/agc-bybit-3m --config configs/volume_alpha.default.yaml volume-backtest
.\.venv\Scripts\python.exe -m aggression_carry --data-root data/agc-bybit-3m --config configs/volume_alpha.default.yaml volume-grid --workers 0
```

Reports:

```text
data/agc-bybit-3m/reports/volume_alpha_report.md
data/agc-bybit-3m/reports/volume_backtest_report.md
data/agc-bybit-3m/reports/volume_backtest_trades.csv
data/agc-bybit-3m/reports/volume_grid_report.md
data/agc-bybit-3m/reports/volume_grid_results.csv
```

Large Bybit downloads are resumable. The downloader prints one line per
symbol/dataset and writes completed chunks immediately. If PowerShell is stopped
with Ctrl+C or a network timeout kills the command, rerun the same command; rows
already completed will show as `cached`.

## 6. Run The One-Year Concurrent Grid

On a 5950X:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_agc_1y_grid.ps1 -Workers 32
```

If the machine starts swapping or RAM pressure gets high:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_agc_1y_grid.ps1 -Workers 16
```

## 7. Run The 1m Daily-Close Fade Grid

This is a separate short-only top-gainer fade test. It downloads 1m klines, so
start with the default 3-month window before trying a full year.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_daily_close_fade_1m.ps1 -Workers 32
```

If RAM pressure gets high:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_daily_close_fade_1m.ps1 -Workers 16
```

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
