# Journal

## 2026-05-02

- Added an isolated daily volume-alpha research path.
- Ran the corrected 3-month Bybit sample.
- Found that increasing-volume variants failed, while `dollar_volume_rank` was
  the only useful lead.
- Officially stripped the repo down around the single-alpha rebuild:
  - removed old live runtime files
  - removed old root backtest/replay/report/runtime modules
  - removed old composite aggression/carry/momentum/quality/OI modules
  - removed tests that only protected deleted behavior
  - simplified config, CLI, docs, and Windows runner around `volume-alpha`

## 2026-05-01

- Installed and documented Codex companion tools: Composio skills, Caveman
  skills, Graphify, AO, Composio CLI, GitHub CLI, and tmux.
- Generated a Graphify code graph for the repo.
- Added `docs/bybit_aggression_carry_system_codex_spec.md`.
- Added the first `aggression_carry/` research package with fixture data,
  Bybit download skeleton, archive parsing, signed-flow aggregation, Parquet
  storage, alpha reports, and costed portfolio tests.
- Fixed Bybit REST pagination and archive handling.
- Added Windows setup and 3-month run scripts.
