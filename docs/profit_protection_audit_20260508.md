# Profit Protection Audit 2026-05-08

## Verdict

The +16,991.58% daily-close-fade benchmark was mechanically produced by the
backtester, but it is not valid alpha evidence. The adaptive profit-protection
logic was warm-starting trailing/MFE state from prices seen before protection
was allowed to activate. That let the backtest exit at stale stop prices as
soon as the post-TWAP delay ended, which is not an executable live behavior.

Do not promote the daily-close fade from the legacy current-top-160 benchmark.
The 2026-05-09 PIT top-50 causal candidate is separate evidence produced after
this fix; it does not rehabilitate the old headline run. This audit is the
repo's reference case for
`docs/backtesting_errors_we_never_repeat.md`, especially the warm-started state
rule.

## Legacy Failure Evidence

The suspicious behavior was real in the legacy artifacts:

```text
Old headline run:              +16,991.58%
Legacy delayed-hard-stop run:  647 / 750 trades exited by post-TWAP minute 16
Legacy immediate-stop run:     647 / 750 trades exited by post-TWAP minute 16
Legacy basket clustering:      342 / 435 baskets had every trade exit by minute 16
```

Those exits clustered exactly where profit protection became active after the
final TWAP add plus the 15-minute delay. The clustering was not a harmless
reporting artifact; it was the main reason the equity curve looked clean.

## Root Cause

The backtest tracked one `best_price` from entry onward. For shorts, this means
the lowest low seen during the 22:00-23:00 TWAP and the 15-minute delay could
arm a trailing or MFE giveback exit even though adaptive exits were not active
yet. At activation, the code could then fill a trailing/giveback stop based on
a pre-activation low.

Live execution cannot depend on a protection state that did not exist yet.
Adaptive state must start when profit protection becomes active.

## Code Fix

The corrected logic keeps two concepts separate:

```text
best_price: full-trade favorable excursion diagnostic
protection_best_price: trailing/MFE state observed only after protection activates
```

The hard disaster stop still starts immediately from the first fill. Adaptive
profit exits now start from `profit_protection_active_time` and do not inherit
pre-activation lows or MFE.

The same correction was applied to paper forward-test marker logic so paper
state cannot silently reintroduce the warm-start behavior.

## Corrected Recheck

The corrected default rerun on the same current-top-160 feature set produced:

```text
Trades:                         750
Baskets:                        435
Total return:                   -4.75%
Sharpe-like:                    0.22
Max drawdown:                   -53.15%
Exits by post-TWAP minute 16:   126 / 750
Median post-TWAP hold:          27m
```

That is a rejection, not a promotion.

## Corrected Grid

A constrained corrected grid tested 216 variants across:

```text
hold_minutes: 60, 120, 180
profit_protection_delay_minutes: 0, 15, 30
vol_trailing_stop_mult: 0, 0.25, 0.5, 1.0
MFE activation/giveback settings
```

Result:

```text
Variants positive in all train/validation/OOS splits: 0 / 216
Best split-ranked variant: pp-0170
Best total return: +39.12%
Best train split: -22.87%
Best max drawdown: -40.79%
```

The best variant was MFE-only with `hold=180m`, `profit_delay=15m`,
`mfe_activation=1%`, `giveback=20%`, and `vol_trail=0`. It still failed the
train split and had unacceptable drawdown.

## Artifacts

Corrected artifacts:

```text
data/research_reports/profit_protection_recheck_20260508/corrected_profit_protection_summary.json
data/research_reports/profit_protection_recheck_20260508/corrected_profit_protection_grid.csv
data/research_reports/profit_protection_recheck_20260508/corrected_profit_protection_top25.csv
data/research_reports/research_log/runs/20260508-175445-daily-close-fade-profit-protection-recheck.md
```

Legacy artifacts retained only for forensic comparison:

```text
data/research_reports/daily_close_twap_2200_2300_current_top160_20260504/
data/research_reports/daily_close_twap_2200_2300_current_top160_20260504/entry_risk_audit/
```

## Decision

No close-fade variant was promoted from this audit. Any future close-fade work
must use corrected non-warm-start profit protection, point-in-time universe
membership, archive-derived 1m bars, split survival, and forward/demo
slice-level TWAP accounting before it can be discussed as real-money ready.

Follow-up:

```text
docs/daily_close_fade.md
```
