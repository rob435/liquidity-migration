# R6 — Per-name, per-bar cost model — VERDICT (code complete; calibration queued)

**Date:** 2026-05-29
**Pre-reg:** [integrated-strategy-program.md](integrated-strategy-program.md) sub-phase R6.
**Run label:** code/infrastructure (no strategy-P&L claim; the recost it enables is
labelled per-cell at Tier-2/Tier-3).
**Code:** `liquidity_migration/cost_model.py` (chunks 1–2, commits `97e110f`, `eb2d58f`).
12 unit tests (`tests/test_cost_model.py`). ruff clean; 1021 pytest pass.

## Headline

**R6 cost-model CODE is COMPLETE and test-gated.** The regression surface, OLS
calibration, per-trade prediction, and ledger recosting (model-vs-legacy side-by-side)
all ship as a reusable library. **β-calibration from live data is DATA-GATED** — the
matched demo/paper-shadow trades it regresses live on the deployment VPS, not the
research box (`data/bybit-demo-event` / `data/bybit-paper-event` are ABSENT here) — so
it is **queued with a turnkey recipe** (below). Until calibrated, the model defaults to
a flat **15 bps taker round-trip**, which is the M2-hardened honest cost and is **never
cheaper** than deployed taker execution.

## What shipped (buildable now)

| Piece | Function | Status |
|---|---|---|
| Cost surface | `CostModelParams` (α + β·{size/ADV, vol, spread, hour, funding}) | ✅ |
| Prediction | `predict_cost_bps` (functional form, floored at the taker round-trip) | ✅ |
| Calibration | `fit_cost_model` (per-venue OLS + diagnostics; safe degrade on thin data) | ✅ |
| Recosting | `recost_trades` (swap the cost leg; keep gross+funding; legacy bps recovered) | ✅ |
| Aggregation | `summarize_recosting` (per-cell model-vs-legacy table) | ✅ |
| Default params | flat 15 bps taker (== `CostConfig.base_entry_exit_cost_bps` at `maker_fill_probability=0`) | ✅ |

Additive — nothing wired into the engine hot path, all defaults unchanged.

## The cost model

```
predicted_cost_bps = α + b_size*(size_usd/ADV_30d) + b_vol*realized_vol_7d
                   + b_spread*spread_proxy_bps + b_hour*hour_norm
                   + b_funding*(funding_rate*hold_hours/8)
```
applied per trade, clamped ≥ the taker round-trip floor (a backtest must never assume
cheaper-than-deployed fills — errors #6/#7/#22). `recost_trades` keeps each trade's
`gross_return` + `funding_return` fixed and substitutes only the cost leg:
`model_net_return = gross_return − notional_weight*model_cost_bps/1e4 + funding_return`.

## Calibration recipe (QUEUED — run on the VPS when ≥30d matched data exists)

The pieces are all in place; calibration is a short wiring once live data is reachable:

1. Collect ≥30d of demo + paper-shadow ledgers (VPS `data/bybit-demo-event`,
   `data/bybit-paper-event`).
2. `reconciliation.reconcile_paper_demo(paper, demo)` already pairs trades by
   id/signal/entry and computes per-trade entry+exit **slippage bps** — that IS the
   realized cost the model regresses (`realized_cost_bps = entry_slippage + exit_slippage`,
   plus the funding diff).
3. Attach the cost features per matched trade (size/ADV at entry, `realized_vol_7d`,
   `hour_norm`, `funding_term`).
4. `fit_cost_model(observations, venue=…)` → calibrated `CostModelParams` + diagnostics.
5. **Validation gate (pre-registered):** predicted-vs-realized corr > 0.5 (R² > 0.25);
   α recovers the venue taker fee (sanity); OOS (last 30d) RMSE within 20% of in-sample.
   If a β fails, drop it and refit.
6. Persist params; `recost_trades` then differentiates cost by name/size/funding.

## Recosting Round 2 cells (the R10 hook)

`recost_trades` + `summarize_recosting` deliver the pre-registered requirement that
**every cell clears its gate under the model cost, not just legacy flat** — reported
side-by-side per cell. Until β-calibration lands, the model cost == the honest 15 bps
taker round-trip, which already **supersedes the Round-1 `cost_multiplier=3` (45 bps)
over-count**: every R1 cell was over-costed ~3× under the legacy flat assumption.
Applying the honest 15 bps is part of the **`9f52819` re-baseline**, so the per-cell
model-vs-legacy delta is produced in the **R9 run-up** (where the carry-forward cells
re-run at the hardened base and are recost on this model), not as a separate sweep now.

## Deferred (with rationale)

- **`--cost-model {flat,model}` engine flag:** deferred. With default (uncalibrated)
  params the model == the flat cost, and the daily backtest cannot observe intrabar
  spread, so a live flag adds nothing until calibration. The post-hoc `recost_trades`
  path covers the R10 "clear under model cost" need. Build the flag only if a phase
  needs in-loop model cost (e.g., calibrated + continuous C-phase).
- **E3/E4 down-payment** (`exit_cost_multiplier`, `maker_fill_probability=0`) already
  shipped earlier and in `9f52819`; R6 builds the full surface on top.

## DECISION

- **R6 CODE COMPLETE.** Cost-model library shipped + test-gated; recost path ready for
  R7/R8/R9/R10.
- **β-calibration + per-cell model-vs-legacy delta: QUEUED** — calibration is
  data-gated on ≥30d VPS demo/paper (turnkey recipe above); the cell delta folds into
  the R9 run-up re-baseline. Neither blocks building R7/R8 (they recost on the honest
  default until calibration refines it).

## Next

R12 sniper entry (R12a/b code) → C0–C3 continuous engine → R7 stress (needs R4+R6 ✓) /
R8 capacity (needs R6 ✓) → R9 integrated assembly + the R1/R13/R5/R2 re-baseline →
R10 demo-candidate gate → R11 pre-2023 OOS.
