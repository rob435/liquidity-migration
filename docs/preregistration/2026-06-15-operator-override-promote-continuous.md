# Decision receipt: operator-override promotion of CONTINUOUS (incl. BTC-vol regime-hedge)

**Date:** 2026-06-15
**Author / authorization:** operator (karlwitney183) — explicit instruction to
"promote that officially" and, when shown the conflict, explicitly chose the
**override: add to `promoted.PROFILES`** option.
**Label:** `EXPLORATORY` / **operator-override registry change — NOT a demo-arbiter
gate pass.** Must never be cited as promotion evidence; it records an operator
decision, not new alpha.

## What this is

The CONTINUOUS-fade book — the operator-approved continuous_ensemble_v2 ensemble + BTC+ETH
2f hedge **including the BTC-vol regime-hedge overlay**
(λ=0.5; `continuous_regime.FROZEN_BTCVOL_REGIME`) — is added
back to `promoted.PROFILES` as `"continuous"`, alongside `"long"`.

Current supersession: on 2026-06-18 the operator froze the current three-component object and
reset the continuous forward clock. Source of truth for the current promoted-in-code
object is still `continuous_forward_replay.FROZEN_FORWARD_CONFIG`, now the
3-component p3/p4p3/p4p5 object. `promoted.continuous_profile()` returns a deep
copy of it.

## Why this needs a loud caveat (the honest evidence state)

This promotion was made **by operator override, NOT by clearing the gate**. At the
time of promotion:

- **Demo/paper ONLY. `REAL_MONEY` stays false. NOT validated for real money.**
- **Tier-2 demo-candidate bar NOT met** — the regime-hedge is pooled MAR delta
  **+0.078 < the +0.1 Tier-2 threshold** in the historical validation record.
- **Tier-3 real-money gate UNMET and UNCHANGED** — needs ≥30 days forward demo,
  both-venue forward MAR>0, drawdown <50%, daily reconciliation, bootstrap pooled
  MAR-delta left tail ≥0, residual Sharpe ≥+0.3, stress + capacity. The overlay
  went live on demo/forward only on 2026-06-15 (forward evidence ≈ 0 days).
- The regime-hedge is the one robust both-venue edge retained from the historical
  continuous program, but it is a
  **modest, sub-period-variable tail-insurance overlay** (~+0.05–0.08 pooled MAR,
  return-additive, beats the random-regime control) — framed as **squeeze
  protection**, not a smooth uniform +MAR edge. Binance has thin 1.5×-cost headroom.

The Tier-3 criteria themselves are **not loosened** by this change (Non-Negotiable
#5). This receipt and the `continuous_profile()` docstring preserve the true state so
"promoted" is never silently read as "real-money ready."

## Files changed (same PR)

- `liquidity_migration/promoted.py` — new `continuous_profile()` accessor; added
  `"continuous"` to `PROFILES`; module docstring updated with override provenance.
- `tests/test_promoted_profiles.py` — registry pin now `{"long","continuous"}`; new
  `test_continuous_profile_is_deployed_book_with_regime_hedge`; candidate-manifest
  tests now assert each manifest is NOT a promoted profile (it is the deployed book
  that got promoted, not those research candidates).
- `tests/test_equity_curves_runner.py` — `PROFILES` pin updated to include continuous.
- `liquidity_migration/continuous_demo.py`, `scripts/equity_curves.py` — stale
  "continuous is outside PROFILES / not a promotion claim" comments corrected.
- `STATE.md` — status and real-money gate annotated with the override.

No runtime/deploy behavior changes: nothing iterates `promoted.PROFILES` to deploy;
the equity tool runs continuous via its own refresh runner. This is a registry/label
change only.

## Backtest produced alongside this change (forced fresh, no cached reports)

Window clipped to data ends on this dev box.

- **LONG (bybit_full_pit), `full_pit_universe` (clean):** 2023-06-15→2026-06-03,
  ret +32.9%, maxDD −3.5%, Sharpe 1.89, MAR 9.5 (ret/DD) / 2.9 (CAGR/DD), 188 trades,
  profit factor 2.08, funding modeled 100%.
- **LONG (binance_full_pit), `full_pit_universe_funding_missing`:** 2023-06-15→
  2026-04-30, ret +29.0%, maxDD −4.0%, Sharpe 1.55, 187 trades. Funding NOT charged
  (optimistic on cost) — data caveat, not citable as fully costed.
- **CONTINUOUS (bybit_full_pit): BLOCKED** — funding-integrity guard rejected the
  root's funding dataset (85 symbols sampled finer than declared settlement interval
  → hourly-snapshot scrape; exact-stamp dedup would over-charge funding ~N× and
  flatter a short book). Correct refusal; fix = rebuild bybit funding via
  `download-data` from the funding-history endpoint.
- **CONTINUOUS (binance_full_pit):** funding on this root was missing/non-overlapping
  for the window, so any continuous number from this snapshot was funding-uncosted
  and demo/shape evidence only. Current continuous conclusions are consolidated in
  `docs/research_summary.md`.

**Methodology debt opened by this promotion:** the continuous book cannot currently
be produced as a *fully-costed* backtest on this dev box (bybit funding contaminated,
binance funding missing). The promotion is therefore a deployment-registry decision
resting on prior in-sample regime evidence + the live forward clock, not on a fresh
fully-costed both-venue backtest. Rebuild funding on both roots before citing any new
continuous backtest as costed evidence.
