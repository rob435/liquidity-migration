# Pre-registration: Binding Tier-2 validation of the deployed BTC-trend gate

**Date:** 2026-06-09
**Author:** claude (for owner)
**Stage:** run-pending

## What's changing

Nothing in code. This is the **binding Tier-2 r1_robustness battery** for the
ALREADY-DEPLOYED `btc_trend_gate=uptrend` on the promoted short profile
(`drop_all_4 + age300 + ff6`), which was deployed 2026-06-04 operator-directed
AHEAD of this validation. Cells: gate `off` (control, `00_baseline`) vs gate
`uptrend` (`01_uptrend`), both venues, standard window 2023-04-01 → 2026-05-28,
exact deployed profile via `liquidity_migration.promoted.short_profile`.

## Hypothesis

The fade is a risk-on edge: alt liquidity-migration pops fade reliably only when
BTC's causal trailing-30d trend (lagged 1d) is positive; in downtrends the edge is
~zero and squeeze risk dominates. Gating entries on the uptrend should keep most
return while cutting drawdown materially (the 06-04 Bybit EXPLORATORY read:
ret +84.7%→+81.0%, DD −15.6%→−7.2%, ret/|DD| 5.42→11.33).

## Predicted direction + magnitude

- Bybit: MAR Δ strongly positive (DD roughly halves, return keeps ≥90%).
- Binance: UNKNOWN and flagged fragile in the original gate pre-reg — this is
  the test that matters. Failure mode: Binance return goes ≤0 under the gate, or
  Binance MAR Δ < −0.5, or pooled MAR Δ ≤ +0.1.
- Trade counts: gated cells lose downtrend-period trades; cell must still clear
  ≥30 (Bybit) / ≥20 (Binance).

## Roots that will be touched

- [x] bybit_full_pit (reports only — no dataset mutation)
- [x] binance_full_pit (reports only — no dataset mutation)
- [ ] forward demo/paper (the gate is already live on demo; this run decides
      whether it stays)

## Decision rule (a priori)

Apply the STATE.md Tier-2 bar verbatim via `scripts/r1_robustness.py`
(MAR-primary, pooled):

- **DEMO-ELIGIBLE** → gate is Tier-2 validated; stays deployed; STATE.md updated.
- **FALSIFY** (return ≤0 a venue, or pooled MAR Δ ≤0, or DD>70%) → recommend
  immediate revert of the deployed gate to `btc_trend_gate="off"` (rollback path
  per STATE/memory); the gate may not be presented as validated.
- **descriptive** (positive but short of the bar) → gate stays an
  operator-directed deploy, explicitly NOT Tier-2-validated; surfaced to operator
  with the fragility diagnostics for a keep/revert decision.

No threshold may be moved to rescue the result. Fragility diagnostics
(thirds/LOO/bootstrap) are reported, non-blocking.

## Run command

```bash
export POLARS_MAX_THREADS=6  # 16GB box
.venv/bin/python scripts/btc_trend_gate_run.py --gate off     --root ~/SHARED_DATA/bybit_full_pit   --start 2023-04-01 --end 2026-05-28 --out ~/SHARED_DATA/bybit_full_pit/reports/btc_gate_tier2_2026-06-09/00_baseline
.venv/bin/python scripts/btc_trend_gate_run.py --gate uptrend --root ~/SHARED_DATA/bybit_full_pit   --start 2023-04-01 --end 2026-05-28 --out ~/SHARED_DATA/bybit_full_pit/reports/btc_gate_tier2_2026-06-09/01_uptrend
.venv/bin/python scripts/btc_trend_gate_run.py --gate off     --root ~/SHARED_DATA/binance_full_pit --start 2023-04-01 --end 2026-05-28 --out ~/SHARED_DATA/binance_full_pit/reports/btc_gate_tier2_2026-06-09/00_baseline
.venv/bin/python scripts/btc_trend_gate_run.py --gate uptrend --root ~/SHARED_DATA/binance_full_pit --start 2023-04-01 --end 2026-05-28 --out ~/SHARED_DATA/binance_full_pit/reports/btc_gate_tier2_2026-06-09/01_uptrend
.venv/bin/python scripts/r1_robustness.py --sweep-tag btc_gate_tier2_2026-06-09
```

## Post-run results

All four cells ran **full-PIT clean** (`run_label: full_pit_universe`) after a one-day
coverage gap (WDCUSDT 2026-05-29, a `bybit_v5_listing`-sentinel manifest row) was
backfilled — the first attempt was TAINTED and was discarded. The local Binance root
ends 2026-04-30, so its cells ran the clipped window (within-venue comparisons
consistent; noted). Code at commit 5e1c960 (working tree).

| cell | trades | return | daily DD | engine MAR | Sharpe |
|---|---:|---:|---:|---:|---:|
| bybit 00_baseline (off) | 596 | +84.7% | −15.6% | 1.38 | 1.43 |
| bybit 01_uptrend | 369 | +81.0% | −7.2% | **2.89** | 1.75 |
| binance 00_baseline (off) | 307 | +18.7% | −17.0% | 0.34 | 0.62 |
| binance 01_uptrend | 181 | +8.1% | −11.8% | 0.22 | 0.40 |

`r1_robustness.py --sweep-tag btc_gate_tier2_2026-06-09`:

- **Verdict line: `01_uptrend  by MARΔ +1.52  bn MARΔ −0.12  pooled +0.70 → DEMO-ELIGIBLE`**
- Bybit fragility: all cell-thirds positive; bootstrap MAR Δ p5 +0.71, P(Δ>0) 98%;
  LOO flips the (tiny) return-delta sign on 2025-04 — the gate's case is MAR, not return.
- Binance fragility: MAR Δ −0.12 (within the −0.5 floor); bootstrap MAR Δ P(Δ>0) 59%
  (a coin flip), ann-ret Δ P(Δ>0) 38%; final third negative (−4%→−5%). The gate is a
  Bybit risk-reduction story; on Binance it is a wash-to-slightly-negative.
- Trade counts clear the bar (369 by / 181 bn).

Reports: `~/SHARED_DATA/{bybit,binance}_full_pit/reports/btc_gate_tier2_2026-06-09/`.

## Verdict

**DEMO-ELIGIBLE (accepted)** — by the pre-registered rule the deployed
`btc_trend_gate=uptrend` is Tier-2 validated and stays deployed. Honest summary for the
record: the validation is carried by Bybit (the deployed venue) where the gate roughly
doubles MAR with 98% bootstrap confidence; Binance neither falsifies nor supports it.
Tier-3 (real money) remains untouched and strict; the forward demo ledgers stay the
arbiter. The fragility diagnostics above are reported per framework, non-blocking.
