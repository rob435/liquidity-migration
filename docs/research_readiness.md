# Research Readiness

Use this after overnight runs to get one pass/fail view across the current
research gates.

```bash
python scripts/report_research_readiness.py
```

Default inputs:

```text
data/daily-close-fade-pit-20230503-20260503/reports/archive_pit_coverage_report.json
data/agc-bybit-3y-auto150-20230503-20260503/reports/volume_grid_splits/volume_promotion_report.json
data/daily-close-fade-1m-3y-current-top160-20230503-20260503/reports/daily_close_fade_grid_splits/daily_close_fade_promotion_report.json
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

This does not change any trading logic. It only prevents promotion decisions
from being scattered across unrelated reports.
