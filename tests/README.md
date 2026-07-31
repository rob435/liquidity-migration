# tests/

146 test files, grouped to mirror `liquidity_migration/`. A test sits next to
the package it tests, so `tests/venue/` covers `liquidity_migration/venue/`.

Two groups have no package counterpart:

| Path | Subject |
| --- | --- |
| `scripts/` | shell and entry-script contracts under `scripts/` and `deploy/` — the systemd wrappers, `ops.sh`, `deploy_vps_live.sh`, `lib_sleeves.sh`, the reset path, and the Python scripts run as jobs |
| `repo/` | repo-wide invariants that belong to no module: cold-import integrity, Codex/Claude skill mirroring, property invariants, dev tooling |

## Conventions

- Named `test_<module>.py` after the module under test. There is no
  `test_liquidity_migration_` prefix — every test here tests this package.
- Basenames are unique across the whole tree, which is what lets the
  subdirectories work without `__init__.py` files.
- `tests/conftest.py` puts the repository root on `sys.path`; it applies to
  every subdirectory.
- A test computes the repository root from its own depth, so a test in
  `tests/account/` uses `parents[2]` and one in `tests/research/backtest/`
  uses `parents[3]`. Getting this wrong resolves the repo to `tests/` and the
  test fails loudly on a missing file.

## Running

```bash
.venv/bin/python -m pytest -q
```

One group: `pytest -q tests/venue`. One file: `pytest -q tests/venue/test_venue_protection.py`.
`scripts/dev.sh check` runs ruff, mypy and the full suite.

`scripts/deploy_vps_live.sh` runs a focused six-file subset as a deploy
preflight; those paths are written out in the script and must be updated
together with any move here.
