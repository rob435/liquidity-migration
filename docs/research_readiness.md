# Research Readiness

Use this after overnight runs to get one pass/fail view across the current
research gates.

Read this together with `docs/backtesting_errors_we_never_repeat.md`. A
readiness pass is not enough if the run violates the backtesting-error standard.

```bash
python scripts/report_research_readiness.py
```

Default inputs:

```text
data/daily-close-fade-pit-20230503-20260503/reports/archive_pit_coverage_report.json
data/agc-bybit-3y-auto150-20230503-20260503/reports/volume_promotion_splits/*/promotion/volume_promotion_report.json
data/research_reports/profit_protection_recheck_20260508/corrected_profit_protection_summary.json
```

Output:

```text
data/research_reports/readiness/research_readiness_report.md
data/research_reports/readiness/research_readiness_report.json
```

Use strict mode when missing artifacts should fail the job:

```bash
python scripts/report_research_readiness.py --strict
```

Interpretation:

- `pass`: the artifact exists and met its configured gate.
- `fail`: the artifact exists and rejected readiness/promotion.
- `missing`: the report has not been generated yet.
- close-fade readiness is based on corrected non-warm-start profit-protection
  artifacts. Legacy daily-close promotion reports from before 2026-05-08 are
  invalid promotion evidence.

This does not change any trading logic. It only prevents promotion decisions
from being scattered across unrelated reports.

## Artifact Manifest

After the readiness report, write a hash manifest for the key research artifacts:

```bash
python scripts/write_research_manifest.py
```

Output:

```text
data/research_reports/manifest/research_artifact_manifest.md
data/research_reports/manifest/research_artifact_manifest.json
```

Use `--strict` when missing artifacts should fail the command. You can include
extra files with repeated `--artifact path/to/file` or `--artifact-glob
'data/.../reports/*.csv'` arguments.

The default manifest includes the corrected close-fade profit-protection
summary and grid CSV:

```text
data/research_reports/profit_protection_recheck_20260508/corrected_profit_protection_summary.json
data/research_reports/profit_protection_recheck_20260508/corrected_profit_protection_grid.csv
```

## Research Run Log

Every serious overnight run should append a research run record:

```bash
python scripts/write_research_run_record.py \
  --name "overnight-research-suite" \
  --strategy volume \
  --status benchmark \
  --bias current_universe_biased \
  --intent "Automated overnight volume promotion sweep." \
  --config configs/volume_alpha.default.yaml \
  --data-root data/agc-bybit-3y-auto150-20230503-20260503 \
  --artifact-glob 'data/agc-bybit-3y-auto150-20230503-20260503/reports/volume_promotion_splits/*/promotion/volume_promotion_report.json'
```

Output:

```text
data/research_reports/research_log/research_log.md
data/research_reports/research_log/research_log.jsonl
data/research_reports/research_log/runs/<run_id>.md
data/research_reports/research_log/runs/<run_id>.json
```

The Windows overnight suite writes this automatically. The point is not
bureaucracy; it is to preserve intent, constraints, config hashes, artifact
hashes, bias labels, and promotion decisions before we are tempted by a pretty
equity curve.
