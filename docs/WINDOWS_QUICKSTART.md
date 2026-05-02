# Windows Fresh Clone Quickstart

Use this when setting up a fresh Windows PC from zero.

## 1. Install Git And Python

Open PowerShell as your normal user, not Administrator.

```powershell
winget install -e --id Git.Git
winget install -e --id Python.Python.3.11
```

If `winget` is unavailable, install Git from `https://git-scm.com/download/win` and Python 3.11 from `https://www.python.org/downloads/`. During Python install, keep the Python Launcher option enabled.

Close PowerShell completely, then open a new PowerShell window.

Verify both tools:

```powershell
git --version
py -0p
py -3.11 --version
```

If `py -0p` says `No installed Pythons found`, Python is not installed correctly. Reinstall Python 3.11 and make sure the launcher is enabled.

## 2. Clone The Repo

```powershell
cd $HOME\Desktop
git clone https://github.com/rob435/MODEL05042026.git
cd MODEL05042026
git switch main
git reset --hard origin/main
```

## 3. Create The Virtualenv And Install Dependencies

Preferred path:

```powershell
.\scripts\windows_setup.ps1
```

If PowerShell blocks the script because of execution policy, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\windows_setup.ps1
```

Manual equivalent:

```powershell
py -3.11 -m venv --clear .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pytest -q
```

Expected test result:

```text
128 passed
```

If `.\.venv\Scripts\python.exe` is not found, the virtualenv was not created. Run:

```powershell
Remove-Item -Recurse -Force .venv -ErrorAction SilentlyContinue
py -3.11 -m venv .venv
```

Then rerun the install commands.

## 4. Run A Tiny Fixture Check

This does not download Bybit data. It only proves the local pipeline works.

```powershell
.\.venv\Scripts\python.exe -m aggression_carry --data-root .tmp/agc-fixture download-data --fixture
.\.venv\Scripts\python.exe -m aggression_carry --data-root .tmp/agc-fixture build-features
.\.venv\Scripts\python.exe -m aggression_carry --data-root .tmp/agc-fixture alpha-report
.\.venv\Scripts\python.exe -m aggression_carry --data-root .tmp/agc-fixture portfolio-backtest
```

Reports:

```text
.tmp/agc-fixture/reports/alpha_report.md
.tmp/agc-fixture/reports/portfolio_backtest.md
```

## 5. Run The 3-Month Bybit Test

Preferred path:

```powershell
.\scripts\run_agc_3m.ps1
```

By default, this run skips raw public-trade Parquet storage and keeps the signed-flow aggregates needed for features, reports, and portfolio backtests. This saves a lot of disk and memory. To also store raw public trades, run:

```powershell
.\scripts\run_agc_3m.ps1 -KeepRawTrades
```

If PowerShell blocks the script because of execution policy, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_agc_3m.ps1
```

Manual equivalent:

```powershell
.\.venv\Scripts\python.exe -m aggression_carry --data-root data/agc-bybit-3m --config configs/aggression_carry.default.yaml download-data --symbols "BTCUSDT,ETHUSDT,SOLUSDT,XRPUSDT,DOGEUSDT,LINKUSDT,AVAXUSDT,APTUSDT,BNBUSDT,ADAUSDT,DOTUSDT,LTCUSDT,NEARUSDT,OPUSDT,ARBUSDT,INJUSDT" --start "2025-01-01" --end "2025-04-01" --datasets "instruments,klines_1h,klines_5m,funding,open_interest,ticker_snapshots,archive_trades" --archive-url-template "https://public.bybit.com/trading/{symbol}/{symbol}{date}.csv.gz" --skip-raw-public-trades
.\.venv\Scripts\python.exe -m aggression_carry --data-root data/agc-bybit-3m --config configs/aggression_carry.default.yaml build-features
.\.venv\Scripts\python.exe -m aggression_carry --data-root data/agc-bybit-3m --config configs/aggression_carry.default.yaml alpha-report
.\.venv\Scripts\python.exe -m aggression_carry --data-root data/agc-bybit-3m --config configs/aggression_carry.default.yaml portfolio-backtest
```

Reports:

```text
data/agc-bybit-3m/reports/alpha_report.md
data/agc-bybit-3m/reports/portfolio_backtest.md
```

## Notes

- The 3-month run can download many GB and may take a long time.
- `run_agc_3m.ps1` requires the virtualenv to use Python 3.11. If it fails the version check, rerun `.\scripts\windows_setup.ps1`.
- The downloader prints each archive symbol/date while it works.
- Existing archive files are reused, so an interrupted run can usually be restarted with the same command.
- Completed raw-trade and signed-flow Parquet partitions are reused too, so reruns skip finished symbol/day outputs.
- Large archive downloads can still hit network timeouts. The downloader retries failed files automatically with a longer timeout before giving up.
- If Bybit REST rejects your network or region, the download step will fail. Run from a network where Bybit public market endpoints are reachable.
- This is a research backtest path only. It does not place live orders.
