# liquidity-migration

Research and demo/paper execution for crypto-perpetual strategies.

@AGENTS.md

## Read By Purpose

- Governance and evidence: `docs/governance.md`.
- Current operations: `STATE.md`, then `deploy/sleeves.env` and systemd units.
- Research interpretation: `docs/research_summary.md` and the relevant raw
  artifacts.
- Active experiment contracts: `docs/preregistration/INDEX.md` and the named
  preregistration.
- Data provenance: `docs/data_roots.md` and `docs/pit_gate.md`.

Do not copy sleeve status or decision thresholds into this file. Derive live
state from current sources. Skills are task runbooks, not general memory.

## Commands

- Repository diagnostics: `scripts/dev.sh doctor` (add `--json` for tools).
- Full local quality gate: `scripts/dev.sh check`.
- Routine operations: `scripts/ops.sh --help`.
- Package CLI: `python -m liquidity_migration --help` and the selected
  subcommand's `--help`.
- Tests: `.venv/bin/python -m pytest -q`.
- Lint: `.venv/bin/python -m ruff check liquidity_migration tests scripts`.

Before a push, run the relevant focused tests and then the repository lint/test
gates in proportion to the change. Never enable `REAL_MONEY` without a separate,
explicit owner instruction for that exact action.
