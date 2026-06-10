# Pre-registration: LONG sleeve vol-target scale-up (volup) candidate

**Date:** 2026-06-09
**Author:** claude (for owner)
**Stage:** run-complete — accepted (operator-promoted into code 2026-06-09; long
sleeve not redeployed)

## What's changing

`vol_target_max_scale` on the deployed long profile (v11a FC + div):
1.0 (de-risk-only) → 1.25 or 1.5 (mild scale-UP in calm-vol regimes). No signal,
selection, exit, or universe change. The 2026-06-09 structural sweep
(`scripts/long_improve_sweep.py`, EXPLORATORY, full-PIT clean, window
2023-04-01→2026-05-28) found every other structural lever null or harmful
(breadth, hold-extension, trailing/scaled exits, pyramiding) and the TSMOM/Donchian
majors overlay a clean null (dilutes MAR at every weight; corr +0.35) — this is the
single surviving candidate.

## Hypothesis

Moreira-Muir vol-managed exposure: crypto vol spikes are uncompensated, so scaling
exposure inversely to vol raises return per unit drawdown. The deployed div overlay
already de-risks (cap 1.0); allowing the SAME rule to scale mildly above 1 in calm
regimes harvests the symmetric half of the effect. Bybit arm (already run):
return +23.3%→+28.9% (1.25) / +32.3% (1.5), ret/DD 8.33→8.30/7.90, Sharpe unchanged.

## Predicted direction + magnitude (binance, unseen at time of writing)

- Return up roughly proportionally (+15-35% relative) with ret/DD within ~10% of
  baseline. Failure mode: Binance return flat-to-down or ret/DD degrading >10%
  relative — would mean the calm-regime scaling only works on Bybit's calendar
  (regime-fit artifact), reject.

## Roots that will be touched

- [x] bybit_full_pit (reports only)
- [x] binance_full_pit (reports only)
- [ ] forward demo/paper — ONLY if accepted AND operator signs off the profile change.

## Decision rule (a priori)

Using the sweep's summary metric (engine summary, same metric both venues, cells
00_baseline / 30_volup125 / 31_volup150):

- **Accept** a volup variant iff, on BOTH venues: return strictly above baseline AND
  ret/DD ≥ 90% of baseline's.
- If both 1.25 and 1.5 qualify, **pick 1.25** (pre-committed: levered long demo
  sizing was contested; the conservative variant is the only one responsibly
  deployable if an operator later opts into leverage. Under the old 10x stress,
  max_scale 1.5 implied peak gross leverage ~17x, which the stress already showed
  was unsurvivable).
- If neither qualifies on Binance, **reject** and the long sleeve keeps its current
  profile; the sweep's nulls stand as the documented result.

## Run command

```bash
POLARS_MAX_THREADS=6 .venv/bin/python scripts/long_improve_sweep.py --venue bybit   # done
POLARS_MAX_THREADS=6 .venv/bin/python scripts/long_improve_sweep.py --venue binance \
  --cells 00_baseline,11_breadth30_best3,30_volup125,31_volup150
```

## Post-run results

Binance (clean `full_pit_universe_funding_partial` — the known 51-symbol funding
coverage hole, equal across cells):

| cell | trades | return | maxDD | ret/DD | Sharpe |
|---|---:|---:|---:|---:|---:|
| 00_baseline | 193 | +19.0% | −3.1% | 6.13 | 1.46 |
| 30_volup125 | 193 | +23.6% | −3.8% | **6.13** | 1.45 |
| 31_volup150 | 193 | +26.3% | −4.4% | 5.94 | 1.45 |

Rule application: volup125 — return strictly above baseline both venues ✓; ret/DD
100% (bn) / 99.6% (by) of baseline ≥ 90% ✓. volup150 — above ✓; 96.9% / 94.8% ✓.
Both qualify → pre-committed tie-break picks **1.25**.

## Verdict

**accepted (candidate)** — `vol_target_max_scale=1.25` lifts return ~+24% relative on
both venues at unchanged risk-adjusted quality (ret/DD ≥ 99.6%, Sharpe unchanged,
trade set identical — pure exposure timing, no selection change; mechanism is
documented Moreira-Muir vol-management, not mined signal).

2026-06-09 addendum: operator signed off — `vol_target_max_scale=1.25` is promoted
in code (`_v11a_long_native_config`). The long sleeve remains toggled off on the live
box; any levered demo sizing is now explicit opt-in and must pass the projected
full-book initial-margin guard. Forward demo/paper remains the arbiter.
