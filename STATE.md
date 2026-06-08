# Research Program State

**Last updated:** 2026-06-08
This file is live/operational state plus binding decision rules. Research conclusions live in
[docs/research_summary.md](docs/research_summary.md).

## First Read

Read these two files only:

1. `STATE.md` - what is running and what rules bind us.
2. `docs/research_summary.md` - consolidated research findings and next direction.

Old one-off research receipts were consolidated and deleted. Git history is the archive.

## Current Status

Liquidity-migration is research-stage. Nothing is approved for real money.

The current short profile is the code-resolved promoted profile:
`drop_all_4 + age300 + ff6 + btc_trend_gate=uptrend`. It does **not** use rmom;
`liquidity_migration_residual_momentum_max=10.0` is the inactive sentinel. The
continuous work is research-only. The right continuous direction is no longer
"independent system replaces old candidate"; it is "old rebalance engine plus cleaner
independent entry/exit logic."

## What's Running

- **SHORT demo/paper:** frozen promoted short profile:
  `drop_all_4 + age300 + ff6 + btc_trend_gate=uptrend`; rmom inactive at `10.0`.
  It remains demo/paper only.
  Key receipts kept:
  - `docs/preregistration/promote-age-ff6-demo-2026-05-31.md`
  - `docs/preregistration/drop-all-4-promotion.md`
- **LONG demo/paper:** `div` profile. Portfolio/diversification sleeve, not standalone
  real-money proof. Receipt kept: `docs/preregistration/div-promotion.md`.
- **CONTINUOUS:** not promoted. Continuous demo orders are off. No-order paper evidence can
  run only as an evidence collector. Do not present continuous as deployed, promoted, or
  real-money ready.
- **VPS:** Hetzner live host runs demo/paper services. Keep `REAL_MONEY=false`; never enable
  real money without explicit owner instruction.

## Current Research Direction

### Daily Short

Currently used profile: `drop_all_4 + age300 + ff6 + btc_trend_gate=uptrend`.

- Age gate around 300d is robust.
- Rmom is not in the promoted short. Historical rmom work is research-only and not a
  current run instruction.
- Execution timing is not the main lever.

### Continuous

The strongest old continuous object is still the decomposed daily-rebalance candidate:

```text
q25_liq500k_btcup_turn4_pop4_decomp_rebalance_w90_tv25_max4_dd4_trend180_hurdle2
```

Keep what works from it:

- decomposed daily rebalance accounting;
- 90d realized-vol targeting;
- 2.5% target daily vol;
- max 4x scale;
- -4% drawdown half-scale;
- 10 bps resize cost;
- optional 180d strategy-equity momentum hurdle.

The merged test is complete. Keep the better independent trade logic:

- age >= 240d;
- `turn3_pop3` entry trigger;
- crowd cap 2;
- TP10;
- 24h hold;
- no hard stop, no rank-decay exit, no giveback exit by default.

Current cleaner cross-venue continuous research candidate:

```text
q25_liq500k_btcup_turn3_pop3_age240_tp10_crowd2_decomp_rebalance_w90_tv25_max4_dd4
```

Use it **without** strategy-equity momentum. Soft 0.25x, soft 0.5x, and the old hard-off
180d/+2% hurdle all hurt the merged signal; hard-off was especially weak under 2x costs.
Details are consolidated in `docs/research_summary.md`; the per-run continuous receipts
were deleted because continuous is not promoted or paper-ready.

The 2026-06-08 derivatives-positioning frontier rejected causal funding,
premium-index, and mark-index-basis hard filters for this merged stream. The
filters had near-complete coverage but reduced MAR versus the unfiltered control.
The closest return retarget was the unfiltered high-scale rule, not a filter:
Bybit +137.46% / MAR 4.39, Binance +112.77% / MAR 4.70, worst DD -10.00%.
That still fails the +120% both-venue and MAR 6 target.

The current best continuous research lead is the scale/window-interpolated
downtrend-extended ensemble, still research-only:

```text
winner_up_p3_30_p4p3_20_p4p5_30_tp14_20_plus_dt40_turn4p5_premium_decomp_rebalance_w70_tv45_max10_dd4
```

It uses the uptrend weighted ensemble
`turn3p3=0.30, turn4p3=0.20, turn4p5=0.30, age210tp14=0.20`, then adds a 40%
downtrend-only `dt_turn4p5` sleeve filtered to `premium_24h_mean >= 0`. Base
validation: Bybit +265.24% / MAR 7.50 / -11.28% DD / 31-of-38 green months;
Binance +190.87% / MAR 6.84 / -9.06% DD / 29-of-36 green months. Common
both-venue green months are 28/38. 2x cost remains profitable: Bybit +177.28%
/ MAR 5.11; Binance +134.13% / MAR 4.85 / -8.98% DD; common both-venue green
months 24/38. Treat it as a cost-robust research winner, not paper-ready evidence.
Worst-DD equality versus the prior row is within float tolerance. Stricter premium
thresholds, market/BTC micro-context filters, and broad component filters were
tested and rejected as replacements. The later TP14 stress-repair retry under
the accepted 40%/70d engine also failed replacement bars: BTC-filtered TP14
helped 2023-12 Bybit, but broad return/MAR and base drawdown got worse.

Component-specific uptrend filtering also did not replace the winner. Hard
`turn4p5` premium/funding filters cut too much return. Partial premium-positive
`age210tp14` replacement produced a useful risk-stability lead
(`u_tp14f15`: base min MAR 6.57, DD -10.19%, 2x min MAR 4.30), but it worsened
the 2024-12 / 2025-04 stress cluster and is not the default winner. Market-context
component filters improved green-month count/DD but cut Binance return/MAR, so they
are rejected replacements too.

Downtrend micro-context filters also did not beat sign-only premium. A 40%/60d
aggressive row improved return/MAR but missed 2x common-green by one month; the
subsequent scale/window interpolation found that `0.4` downtrend scale with a 70d
vol window recovers the strict 2x common-green bar while preserving the return/MAR
improvement.
All June 7-8 continuous run receipts are consolidated in `docs/research_summary.md`;
the durable artifacts remain under `C:\Users\user\SHARED_DATA\...`.

## Binding Decision Rules

Forward demo/paper is the arbiter. MAR is primary; Sharpe is secondary.

### Tier 1 - Investigation

- MAR delta positive on majority venues, or one venue positive with the other not badly worse.
- No return sign-flip versus control.
- At least 30 Bybit trades and 20 Binance trades, unless explicitly labeled a tiny scout.

### Tier 2 - Demo Candidate

- Positive return on both venues.
- Pooled MAR delta > +0.1.
- Neither venue worse than MAR delta -0.5.
- Trade counts clear Tier 1.
- Fragility diagnostics are reported, not used to rescue weak cells.

### Tier 3 - Real Money

Strict and not loosened:

- At least 30 days forward demo/paper evidence.
- Forward MAR > 0 both venues.
- Drawdown < 50%.
- Daily paper/demo reconciliation.
- Bootstrap pooled MAR-delta left tail >= 0.
- Residual Sharpe >= +0.3.
- Stress pass and capacity >= 10x deployment size.

No internal pre-2023 OOS substitute exists.

## Methodology Debts

These can still move numbers:

- Binance funding interval handling.
- Live age definition versus PIT backtest age definition.
- Residual-momentum causality at decision timestamp.
- Factor/residual day-grid alignment.
- Continuous forward window is immature; current local evidence is not enough.

Risk-model receipt kept: `docs/preregistration/r4-risk-model-verdict.md`.
PIT membership receipt kept: `docs/preregistration/pit-membership-trading-day-fix.md`.

## Helpers

- Reconcile all sleeves: `bash scripts/reconcile.sh`
- Run daily research cell: `scripts/volume_events_cell.sh --cell-id X --overrides 'KEY=VAL,...'`
- Tier-2 robustness: `python scripts/r1_robustness.py --sweep-tag <TAG>`
- Legacy strict analyzer: `python scripts/apply_decision_rule.py SUMMARY.csv --control 00_baseline`
- Continuous readiness diagnostic: `python -m liquidity_migration continuous-forward-readiness --paper-only`
- Continuous vs daily forward comparator: `python -m liquidity_migration continuous-vs-daily-forward`

## Non-Negotiables

1. Never set `REAL_MONEY=true` without explicit owner instruction.
2. Never present continuous as promoted or paper-ready.
3. Both venues matter; single-venue Bybit wins are not enough.
4. Full-PIT, causal features, ledgers, and cost modeling are correctness gates.
5. Do not loosen Tier 3 to rescue a result.
6. Pre-push gate before any push: ruff plus pytest.
7. Do not commit or push without operator confirmation.

## How To Update

Keep this file short. Put research results in `docs/research_summary.md`. Keep
`docs/preregistration/` small and only for receipts that still bind an active
deployment, candidate, or methodology decision.
