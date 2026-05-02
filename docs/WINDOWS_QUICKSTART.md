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
```

Report:

```text
.tmp/volume-fixture/reports/volume_alpha_report.md
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
```

Report:

```text
data/agc-bybit-3m/reports/volume_alpha_report.md
```

## Notes

- The current official path is volume-only. It does not download Bybit public
  trade archives.
- The old `build-features`, `alpha-report`, `portfolio-backtest`, and
  `research-sweep` commands were removed with the old composite stack.
- This is a research backtest path only. It does not place live orders.
- If Bybit REST rejects your network or region, the download step will fail. Run
  from a network where Bybit public market endpoints are reachable.
