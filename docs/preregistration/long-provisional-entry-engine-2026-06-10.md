# Pre-registration: PE2 — engine-grade provisional trigger-hour entry (long FC)

**Date:** 2026-06-10 (registered BEFORE the run). **Label:** `exploratory`; adoption
only on the full bar below. **Parent:** PE1 (`long-provisional-entry-2026-06-10.md`)
FAILED its Stage-0 tail bar but defined this exact revival path: engine-grade,
ATR stops ACTIVE FROM ENTRY (capping the tail that failed), judged against the REAL
engine lifecycle. Operator authority: the standing 2026-06-10 long-sleeve directive.

## Mechanism (one new boolean, zero new tuned parameters)

`fc_provisional_entry=True` adds, on top of the deployed v11a+div+volup125 profile:

- **Trigger panel (causal, scout-validated construction, engine-native gates):** at
  each hourly close h inside day D: trailing-24h log return ≥ the SAME threshold the
  daily FC trigger uses (2.5σ_daily(30d) from day D−1, floor log1p(0.15)),
  trailing-24h close-location ≥ 0.7 (the deployed `fc_min_close_location`),
  trailing-24h turnover rank ≤ 10 (the deployed `fc_top_volume_rank_max`),
  `in_universe` + BTC/ETH regime gates read from the D−1 feature row. Per-symbol
  cluster dedup (24h).
- **Provisional entry at h's bar close** under all standard portfolio gates
  (capacity, cooldown, per-symbol caps), sized by the standard vol-parity ×
  vol-target path using the D−1 feature row; ATR stop/TP (`_fc_exit_params` on the
  D−1 row) live IMMEDIATELY; max-hold anchored at the provisional entry.
- **Confirmation at D's daily close:** if the standard daily FC classification
  fires for that symbol/day → the position simply continues (the daily path skips
  it as already-held); else → cut at the daily-close bar, reason
  `unconfirmed_cut` (same convention PE1 measured). Cuts set the standard 7d
  cooldown (engine semantics, stated: this can suppress later confirmed entries —
  part of the policy under test).
- Daily-confirmed events with no hourly trigger (e.g. 3d/7d cumulative triggers) or
  blocked provisional entries enter via the UNCHANGED standard sniper-retrace path.
- `fc_provisional_entry=False` paths are byte-identical to the deployed engine
  (regression bar: the full test suite + an unchanged baseline rerun).

## Declared cells (final menu)

`00_baseline` (existing artifacts) vs `LR30_prov` (`fc_provisional_entry=True`),
both venues, window 2023-04-01→2026-05-28, research gates ON. Sensitivity on the
prov cell only: `cost_multiplier=2`.

## A-priori adoption bar (ALL, both venues, clean full-PIT)

1. ret/DD ≥ 1.10× baseline.
2. Total return ≥ 0.90× baseline.
3. Worst exit-day not worse than baseline by > 1pp.
4. MTM active-day fraction ≥ baseline (the operator's time-in-market ask).
5. 2× cost: return stays > 0 and ret/DD ≥ baseline at 2× on both venues.
6. Per-year splits: no new always-negative year.

PASS → promote `fc_provisional_entry=True` into `_v11a_long_native_config` with this
receipt (volup125 precedent); the live daemon inherits it only at the operator's
next long-sleeve deployment decision (sleeve currently OFF).
FAIL → the long-sleeve frequency question closes COMPLETELY (LR + PE1 + PE2);
record and stop — no further variants of any kind on this window.

## Verdict (filled in after the run) — FAIL on bars 1+6 (binance); the frequency question CLOSES

All runs clean `full_pit_universe`, deployed profile incl. its built-in
`cost_multiplier=3.0` (every number below is at the 3× cost stress).

| metric | bybit base → prov | binance base → prov | bar |
|---|---|---|---|
| trades | 192 → 352 | 195 → 345 | — |
| return | +28.9% → +38.6% (1.34×) | +22.8% → +24.5% (1.07×) | 2 ✓✓ |
| ret/DD | 8.30 → **12.75 (1.54×)** | 5.90 → **6.42 (1.088×)** | 1 ✓ / **✗ (<1.10×)** |
| worst exit-day | −1.50% → −1.09% | −1.51% → −1.12% | 3 ✓✓ |
| MTM active days | 27.6% → 32.6% | 27.0% → 31.8% | 4 ✓✓ |
| MTM Sharpe | 1.70 → 1.80 | 1.34 → 1.28 | — |
| per-year | all positive | **2026: +1.3% → −1.2%** (partial yr) | 6 ✓ / **✗** |

Bar 5 not evaluated: the declared cost cell was MIS-SPECIFIED (set
`cost_multiplier=2.0`, which is CHEAPER than the deployed 3.0 — caught because
"2× cost" returns came out HIGHER, a stop-work anomaly explained by code; the cell
is a cost-reduction sensitivity, reported as nothing). Moot given bars 1/6.

**FAIL → per the pre-registered clause, the long-sleeve frequency question closes
COMPLETELY (LR + PE1 + PE2). No adoption; no further variants of any kind on this
window.** The mechanism is real and the engine implementation works exactly as
designed (ATR stops from entry capped the PE1 tail: worst-day IMPROVES both
venues; +5pp time-in-market both venues; bybit transformed). What kills it is the
program's spine: the binance edge is too thin to clear a conjunctive cross-venue
bar (1.088× vs 1.10×) and its partial-2026 flips slightly negative. Bybit-strong /
binance-marginal is the documented venue-composition structure (CV1 / Binance-gap
decomposition), not a new fact. `fc_provisional_entry` stays in the engine,
default OFF, fully tested — a future program with fresh forward data (or the
shared-universe profile idea from the Binance-gap receipt) may re-judge it on NEW
evidence, never by re-mining this window.
