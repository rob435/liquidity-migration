# Journal

## 2026-05-01

- Installed and documented Codex companion tools: Composio skills, Caveman skills, Graphify, AO, Composio CLI, GitHub CLI, and tmux.
- Generated a Graphify code graph for the repo.
- Cleaned local generated artifacts: Python bytecode, pytest cache, local SQLite runtime DB files, old backtest output directories, and the warmed 4.7GB candle cache.
- Removed stale source-tracked docs and deployment files from the old strategy:
  - `SPEC.md`
  - `BACKTEST_RESEARCH_PLAN.md`
  - `full_env.txt`
  - stale production env, systemd, launchd, sync, soak, and Windows deployment scaffolding under `deploy/`
- Rewrote `README.md`, `STATUS.md`, and `DECISIONS.md` to reflect the reset posture.
- Added `docs/bybit_aggression_carry_system_codex_spec.md` as the authoritative current plan for the Bybit aggression-carry alpha-proof overhaul.
- Added the isolated `aggression_carry/` phase-one research package with fixture data generation, Bybit REST download skeleton, Parquet storage, signed-flow aggregation, feature engineering, alpha reporting, and costed portfolio backtesting.
- Added regression tests for duplicate trade handling, archive/WebSocket trade parsing, Parquet merge/dedupe behavior, no-leakage forward returns, config loading, funding attribution, and missing forward-return accounting.
- Expanded the backtesting layer with research evidence tables, cost-adjusted quantile ledgers, portfolio period/position ledgers, symbol/month attribution, fee-share checks, symbol concentration checks, and best-month/BTC-ETH-SOL exclusion robustness.
- Tried AO spawning; it is blocked until GitHub CLI authentication is completed with `gh auth login`.
- Kept the executable Python modules and tests intact for now so the replacement strategy can salvage useful backtest, exchange, database, and analysis code deliberately.
- Fixed a stale unreachable block in `reconcile.py` that referenced an undefined `export_path`.
- Fixed Bybit public archive handling so downloaded `.csv.gz` files keep their compression suffix, parse archive `timestamp`/`trdMatchID` rows, and can run archive-only without constructing a REST client.
- Verified a real one-day SOLUSDT archive smoke: `560,085` raw trades, `1,440` signed-flow 1m rows, and `24` signed-flow 1h rows.
- Verified with `compileall`, `git diff --check`, and `pytest -q` (`127` passing tests). `pyflakes` is not installed in the active Python.
- Added Windows fresh-clone setup docs plus PowerShell helper scripts for dependency setup and the 3-month Bybit aggression-carry test run.
- Hardened Bybit archive downloads with longer timeouts, retry/backoff, partial-file cleanup, and per-file progress output for long Windows runs.
- Changed archive ingestion to process and write each symbol/day independently instead of retaining all raw trade frames in memory during multi-month runs.
