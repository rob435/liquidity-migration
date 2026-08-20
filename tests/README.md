# tests/

The tree mirrors `liquidity_migration/`: a test sits next to the package it
tests, so `tests/venue/` covers `liquidity_migration/venue/`. Two groups have
no package counterpart:

| Path | Subject |
| --- | --- |
| `scripts/` | shell and entry-script contracts under `scripts/` and `deploy/` — the systemd wrappers, `ops.sh`, `deploy_vps_live.sh`, `lib_sleeves.sh`, the reset path, and the Python scripts run as jobs |
| `repo/` | repo-wide invariants that belong to no module: cold-import integrity, markdown links, Codex/Claude skill mirroring, property invariants, dev tooling |

## Conventions

- Named `test_<module>.py` after the module under test. There is no
  `test_liquidity_migration_` prefix — every test here tests this package.
- No `__init__.py` anywhere; the directories are plain folders.
- Two conftests. `tests/conftest.py` puts the repository root on `sys.path` and
  applies to every subdirectory. `tests/scripts/conftest.py` adds an autouse
  fixture that fences git — each test runs chdir'd into its own tmp dir with
  git discovery and host config blocked, because those tests run the real
  `git` the deploy and reset scripts call.
- A test computes the repository root from its own depth, so a test in
  `tests/account/` uses `parents[2]` and one in `tests/research/backtest/`
  uses `parents[3]`. Getting this wrong resolves the repo to `tests/` and the
  test fails loudly on a missing file.

## Running

```bash
.venv/bin/python -m pytest -q
```

One group: `pytest -q tests/venue`. One file: `pytest -q tests/venue/test_wedged_command_resolution.py`.
`scripts/dev.sh check` runs the full gate — ruff, mypy, the suite, and the
engine's Rust tests — and is what the tracked `pre-push` hook calls.
