# liquidity-migration

Research-stage crypto-perp repo: continuous fade demo book plus long-native v11a.
The daily SHORT sleeve was erased on 2026-06-11 by operator order; git history is
the archive.

@AGENTS.md

## Read First

- `STATE.md` - live state, open operator decisions, and the three-tier gate.
- `docs/research_summary.md` - research decisions, failure ledger, revisit queue.
- `docs/backtesting_errors_we_never_repeat.md` - methodology gate for any
  backtest, result interpretation, or strategy change.
- `docs/data_roots.md` - which root to use and which roots are inert history.

Do not use old receipts as active guidance unless they still exist under
`docs/preregistration/` and are listed as active/binding in the summary.

## Running

- CLI: `python -m liquidity_migration <subcommand>`
- Tests: `.venv/bin/python -m pytest -q`
- Coverage: `.venv/bin/python -m pytest --cov` (needs `pip install -e .[dev]`;
  config in `pyproject.toml [tool.coverage.*]`, package-only, branch coverage —
  ~77% baseline 2026-06-14, no hard gate yet).

Before any push:

```bash
.venv/bin/python -m ruff check liquidity_migration tests scripts
.venv/bin/python -m pytest -q
```

The tracked hook source is `scripts/git-hooks/pre-push`. A fresh clone needs:

```bash
cp scripts/git-hooks/pre-push .git/hooks/pre-push && chmod +x .git/hooks/pre-push
```

## Working Rules

- Never set `REAL_MONEY=true` without explicit owner instruction.
- Continuous is live demo/research-stage, not promoted or paper-ready.
- LONG is demo/paper only and currently toggled off.
- Both venues, full PIT, causal features, costs/funding, ledgers, and
  reconstructable run records are mandatory.
- Use `.claude/skills/` only when the task matches a skill; do not treat skill
  files as general memory.
- `liqmig-research.current_state` returns `STATE.md`; report tools are helpers,
  not a substitute for reading the relevant file.
- For architecture questions, read `graphify-out/GRAPH_REPORT.md` first.
