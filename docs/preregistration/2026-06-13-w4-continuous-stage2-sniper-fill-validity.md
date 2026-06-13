# Pre-registration: W4 Continuous Stage 2 - Sniper Fill Validity

**Date:** 2026-06-13
**Author:** Codex
**Stage:** complete

## What's changing

Measure the current live continuous sniper add-on in the replacement W4 program.
This stage does not search for a wick, size, lifecycle, or adaptive rule. It
tests only the fixed live form:

- one PostOnly short add-on per fresh base entry;
- limit price `entry_price * 1.08`;
- add-on size `0.25 * base_notional`;
- venue-side 25% disaster stop attached to the add-on;
- if filled and not stopped, exit with the base trade lifecycle.

The base-entry population is the Stage 1 `00_frozen_no_stop` control component
trades generated in this W4 replacement program.

## Hypothesis

The fixed +8% quarter-size sniper can be historically measurable as an add-on
without fitting a new wick: enough orders touch, the filled add-on return is
positive on both venues after funding and conservative taker-equivalent costs,
and adverse continuation after touch does not dominate the edge.

Failure mode if wrong: the add-on rarely touches, loses after touch, worsens
MAR materially versus the frozen control, or depends on one venue only. A
failure blocks only this exact fixed live sniper measurement; it does not
authorize a new wick search on the spent window.

## Population

- Window: `2023-04-01 <= signal_ts < 2026-05-01`.
- Roots:
  - `~/SHARED_DATA/bybit_full_pit`
  - `~/SHARED_DATA/binance_full_pit`
- Base entries:
  `~/SHARED_DATA/{venue}_full_pit/reports/w4_continuous_stage1_stop_exit_2026-06-13/00_frozen_no_stop/{component}/continuous_trades.csv`.
- Components: `turn3p3`, `turn4p3`, `turn4p5`, `age210tp14` with the frozen
  Stage 1/continuous ensemble weights.

## Measurement

For each base trade:

1. Create one hypothetical sniper order at `entry_price * 1.08`.
2. A fill is valid only on an hourly bar strictly after the base entry timestamp
   and before the base exit timestamp where `high >= limit_price`.
3. Fill price is the registered limit. The add-on becomes live at the end of
   the touch bar, avoiding same-bar favorable exit credit.
4. A 25% add-on stop exits at `limit_price * 1.25` if any later bar before the
   base exit has `high >= stop_price`.
5. Otherwise the add-on exits at the base trade exit price and timestamp.
6. Funding is summed over `(fill_ts, exit_ts]` from the venue funding dataset.
7. Costs use the base trade's per-notional round-trip cost as a conservative
   taker-equivalent cost. No maker rebate is credited.
8. Add-on return contribution is
   `0.25 * notional_weight * (gross_short_return + funding + cost_per_notional)`.

The adjusted ensemble ledger adds sniper return contributions to the Stage 1
control daily ledger on the add-on exit day. This stage does not refit the BTC
hedge or resize the control book after seeing add-on fills.

## Decision rule (a priori)

The fixed sniper is "historically supported for forward watch only" if all are
true:

- valid fill rate is at least 5% on both venues;
- average net bps per filled add-on is positive on both venues;
- adjusted total return remains positive on both venues;
- R1 pooled MAR delta versus `00_control` is positive;
- neither venue has MAR delta worse than `-0.50`;
- stop-hit filled-order losses do not exceed filled non-stop profits on either
  venue.

It is rejected in this registered form if any venue has non-positive average
filled-add-on bps, adjusted return non-positive, R1 pooled MAR delta <= 0, any
venue MAR delta <= -0.50, or stop-hit losses exceed non-stop profits. If fill
rate is below 5% on either venue, the historical bar evidence is labeled
insufficient rather than alpha-positive.

Forward demo fill evidence remains a separate gate. If live/demo sniper fills
are still zero, Stage 2 makes no forward-validity claim.

## Run command

```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python scripts/w4_continuous_sniper_fill_validity.py \
  --venues bybit,binance \
  --start 2023-04-01 \
  --end 2026-05-01 \
  --out ~/SHARED_DATA/w4_continuous_stage2_sniper_fill_2026-06-13

PYTHONPATH=. .venv/bin/python scripts/r1_robustness.py \
  --sweep-tag w4_continuous_stage2_sniper_fill_2026-06-13 \
  --control 00_control \
  > ~/SHARED_DATA/w4_continuous_stage2_sniper_fill_2026-06-13/stage2_r1_robustness.txt
```

## Required artifacts

- Per-base-trade sniper event rows with fill validity, touch/fill timing,
  stop/exit reason, add-on return, and adverse/favorable path.
- Per-venue adjusted ledgers and monthly returns.
- R1-compatible `volume_event_best_monthly.csv` and
  `volume_event_research_report.json`.
- Stage summary JSON/CSV/Markdown with root identity, code hash, config hash,
  effect sizes, fragility diagnostics, and the registered falsifier.

## Post-run results

Artifacts:

- Stage summary:
  `~/SHARED_DATA/w4_continuous_stage2_sniper_fill_2026-06-13/stage2_summary.{json,csv,md}`.
- Per-order event rows:
  `~/SHARED_DATA/w4_continuous_stage2_sniper_fill_2026-06-13/stage2_sniper_events.csv`,
  plus per-venue splits in the same directory.
- R1 robustness receipt:
  `~/SHARED_DATA/w4_continuous_stage2_sniper_fill_2026-06-13/stage2_r1_robustness.txt`.
- Per-venue ledgers and R1 JSON:
  `~/SHARED_DATA/{bybit,binance}_full_pit/reports/w4_continuous_stage2_sniper_fill_2026-06-13/`.

Run identity:

- Git HEAD: `e7ce8c81ad076a055aa59d64362333024a78c7af`.
- Code hash:
  `7101e93e637618eff2857abf85453fdc3ce0923a64a18be9a927706a4a924725`.
- Frozen forward config hash:
  `1fc760f14567a204d73f36d5ffb81243d40196338ec72f9e7b4f137f431f0017`.
- Full-PIT roots: `~/SHARED_DATA/bybit_full_pit`,
  `~/SHARED_DATA/binance_full_pit`.
- Window: `2023-04-01 <= signal_ts < 2026-05-01`.

Historical fill-validity summary:

| Venue | Eligible | Filled | Fill Rate | Avg Filled Bps | Delta Return | Control MAR | Sniper MAR | MAR Delta | Stop Hit Rate |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `bybit` | 3223 | 1200 | 37.23% | 169.10 | 0.0738 | 4.3957 | 4.9449 | +0.5492 | 11.58% |
| `binance` | 2966 | 992 | 33.45% | 51.08 | 0.0309 | 5.5311 | 5.5143 | -0.0167 | 12.40% |

Stop-hit loss did not exceed non-stop profit:

- Bybit: stop loss return loss `0.1033`, non-stop return profit `0.2192`.
- Binance: stop loss return loss `0.0941`, non-stop return profit `0.1646`.

Component split, return contribution:

- Bybit: `turn3p3=0.0216`, `turn4p3=0.0225`, `turn4p5=0.0196`,
  `age210tp14=0.0102`.
- Binance: `turn3p3=0.0112`, `turn4p3=0.0100`, `turn4p5=0.0064`,
  `age210tp14=0.0034`.

R1 robustness:

- `01_fixed_x8_b25_base_exit` is `DEMO-ELIGIBLE` by the R1 Tier-2 historical
  rule: bybit MAR delta `+0.34`, Binance MAR delta `-0.06`, pooled `+0.14`,
  positive return both venues.
- Third-split return stayed positive on both venues.
- Bybit bootstrap annual-return delta: p5 `+1.8%`, P(delta > 0) `100%`;
  bootstrap MAR delta p5 `-0.85`, P(delta > 0) `81%`.
- Binance bootstrap annual-return delta: p5 `-0.4%`, P(delta > 0) `85%`;
  bootstrap MAR delta p5 `-2.12`, P(delta > 0) `2%`.

The Binance bootstrap MAR weakness is material. This is not Tier-3 evidence and
does not override the forward-fill gate.

## Verdict

HISTORICALLY SUPPORTED FOR FORWARD WATCH ONLY in the exact fixed live form
(`entry * 1.08`, quarter-size, 25% stop, exit with base lifecycle).

This supports retaining the armed demo sniper as a forward evidence collector.
It is not paper-ready, not promoted, and not real-money evidence. Since local
state still has zero live sniper placements/fills, this stage makes no
forward-fill-validity claim.
