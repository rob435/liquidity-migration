# Operator Override: Vol-Gated upper_wick Entry Sizing → Continuous v2 Demo/Paper

Date: 2026-06-20
Author: Claude (at operator/owner direction)
Stage: operator override (promotion into the demo/paper object, code-level)
Scope: continuous v2 fade book, Bybit demo/paper only
Run label: `exploratory` evidence promoted by operator override. **`REAL_MONEY` stays false.**

## Decision

The owner directed promotion of the **vol-gated upper_wick entry-quality sizing tilt** into
the official continuous v2 demo/paper object. This is an OPERATOR OVERRIDE in the same
pattern as the 2026-06-19 TP12 / vol-off override: it promotes on internal (in-sample)
evidence, which the repo's standing gate normally forbids ("Forward demo/paper is the
arbiter. Internal backtests are not promotion evidence."). It is recorded honestly as such.

## What is promoted

A causal, mean-1 per-trade size multiplier applied MULTIPLICATIVELY on top of the existing
inverse-vol sizing: `entry_notional = base × component_weight × invvol_mult × upperwick_mult`.

`upperwick_mult = clip(1 + k·z_uw·att, [0.5, 1.5])`, k=0.5, where
- `z_uw` = per-symbol expanding-prior z-score of the pre-entry 120m mean upper-wick fraction;
- `att` = 1 − (per-symbol expanding-prior percentile of rv) — vol attenuation, since
  upper_wick is empirically blind on high-vol names where inverse-vol already downsizes.

Shared causal implementation: `liquidity_migration/continuous_entry_sizing.upperwick_size_mult`
— called by BOTH the backtest validator and the live demo hook, so live == backtest by
construction (the live↔backtest parity gate).

## Validated evidence (in-sample full-ledger; the basis for the override)

Bybit, full-ledger (real engine `size_mult_lookup` + 2f hedge + rebalance), vol-attenuated:

| arm | MAR | total | max_dd |
|-----|----:|------:|-------:|
| control (inverse-vol only) | 6.387 | 0.2599 | -0.0130 |
| upper_wick ungated | 6.497 | 0.2618 | -0.0129 |
| **upper_wick VOL-GATED** | **6.555** | 0.2600 | -0.0127 |

+0.168 MAR vs inverse-vol alone, +0.058 vs ungated, **+1.62 vs hash**, passes. OOS-validated
(standardize early / validate late), hash-controlled, mechanism-backed (exhaustion/rejection
entries are better fades; the tilt is clean — return without extra tail). Receipt for the full
research arc: `docs/preregistration/2026-06-20-continuous-v2-bybit-entry-alpha-construction.md`.

## Honest caveats (this override is against the standing gate)

- **In-sample.** No forward demo/paper evidence yet. The repo's gate says internal backtests
  don't promote; this overrides that by owner direction.
- **Modest.** +0.168 hedged MAR (~2.6% relative); the 2f hedge dominates the hedged MAR.
- **Bybit-only.** Validated on Bybit; not a both-venue object. (The live demo book is Bybit,
  so it is the applicable venue — but this is not cross-venue-validated.)
- **Voids the forward ledger ON ACTIVATION** (see below) — the config-hash-pinned continuous
  forward ledger accruing since 2026-06-18 must be archived + a fresh clock started when the
  live sizing actually changes.

## What this commit does (code-level promotion, SAFE, live-inactive)

- New `continuous_entry_sizing.upperwick_size_mult` (shared, pure, tested).
- `ContinuousDemoCycleConfig.entry_upperwick_sizing_enabled` (default **False**).
- Live hook `_continuous_upperwick_multiplier` wired into the continuous entry sizing
  (`continuous_demo.py`), multiplying the notional by `cand["upperwick_size_mult"]` ONLY when
  the flag is on AND the feature is populated — a **SAFE NO-OP (1.0)** otherwise.
- Backtest validator refactored to call the shared function (parity by construction).
- Tests: `tests/test_continuous_upperwick_sizing.py` (cold-start no-op, up/down tilt, clip
  bounds, vol attenuation, live no-op-until-enabled-and-populated).

**Because the flag defaults False and the live feature is not yet computed, the live demo
book behavior is UNCHANGED by this commit and the forward ledger is NOT yet voided.** This is
the deliberate, safe maximum that can be promoted without the live infrastructure below.

## REQUIRED to actually trade it live (the honest blocker — separate follow-up)

The live continuous daemon runs an HOURLY feature pipeline (`rv_168h`, `max_ret168`) and has
NO 1m infrastructure. upper_wick needs trailing-120m 1m klines + per-symbol expanding
history at each entry. So activation requires, in order:

1. Build the live 1m upper_wick feature pipeline + per-symbol (upper_wick, rv) history in the
   daemon, populating `cand["upperwick_size_mult"]` via the shared function.
2. Reconcile live-vs-backtest parity (archive 1m may differ from exchange 1m; prove the
   multiplier matches within tolerance) — this is a hard gate, not optional.
3. Flip `entry_upperwick_sizing_enabled=True` in the deployed profile.
4. Archive the continuous forward state dir + start a fresh clock (config-hash change),
   regenerate hedge warmstart, and deploy via the pre-push gate (owner-gated VPS deploy).

Until step 1–4 are done, the override is code-resident but live-inactive.

## No real-money claim

`REAL_MONEY` stays false. Demo/paper only. The Tier-3 real-money gate (forward evidence,
both-venue agreement, reconciliation, stress, capacity, owner authorization) is unmet and
unchanged. A modest in-sample Bybit-only tilt is explicitly NOT real-money evidence.
