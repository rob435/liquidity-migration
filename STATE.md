# Research Program State

**Last updated:** 2026-06-12

Read this first for live state and binding decision rules. Research conclusions
live in [docs/research_summary.md](docs/research_summary.md).

## First Read

1. `STATE.md` - what is running, what is open, and what rules bind us.
2. `docs/research_summary.md` - current research decisions, failure ledger, and
   revisit queue.

Git history is the archive. Do not keep closed one-off receipts in the hot path.

## Current Status

Liquidity-migration is research-stage. **Nothing is approved for real money**
(`REAL_MONEY=false`; demo/paper only). The daily SHORT sleeve was erased from
the system on 2026-06-11 by operator order; only continuous fade and long v11a
remain.

The VPS runs the continuous demo system. LONG is promoted-in-code for demo/paper
only but toggled off in `deploy/sleeves.env`. Continuous is live demo evidence,
not promoted and not paper-ready.

Several 2026-06-12 audit rounds found and fixed live execution bugs
(component-weight sizing, recovery/adoption identity, ws_risk realized PnL,
telegram visibility, deploy pinning, liveness checks, and ledger bucketing).
Details are in git history; the current behavior below is what matters.

## What's Running / Wired

- **Continuous demo book:** live default is `continuous_ensemble_v1`
  (`winner_base`: p3 .30 / p4p3 .20 / p4p5 .40 / tp14 .10,
  w90/tv0.045/max4/ddh-0.04, no momentum hurdle, rmom q25,
  BTC-uptrend gate). Demo fills are execution evidence only.
- **2f BTC+ETH hedge:** wired and armed, but risk-increasing legs are blocked by
  stale warmstart CSVs (`deploy/hedge_warmstart/*.csv`). Flat/no-action runs are
  healthy; failed/blocked armed runs page.
- **Sniper:** wired and armed in demo (`CONTINUOUS_SNIPER=1`; code default off).
  No placements yet because the base book has had zero entries since the
  2026-06-09 rebuild.
- **Dynamic exit:** no-order forward paper shadow only. The in-sample result was
  a cross-venue null; the shadow is the only possible revival path.
- **Shared kline data plane:** paper shadow follows the demo root's flushed
  kline snapshot read-only (`KLINES_FOLLOW_ROOT`).
- **LONG:** `div` + volup125 + weekend 1.5x tilt, toggled off, awaiting operator
  leverage/capital decision.
- **VPS:** Hetzner demo host. A push to the deployment branch can auto-deploy;
  do not push without operator confirmation and the pre-push gate.

## Open Operator Decisions

1. Regenerate `deploy/hedge_warmstart/*.csv` and define refresh cadence, or
   disarm the hedge timer.
2. Enable the Bybit forward depth collector on the VPS if capacity data matters:
   `systemctl enable --now liquidity-migration-depth-collector`.
3. Finish Binance FAPI ancillary June top-ups from the VPS or another permitted
   host; the dev box is region/network blocked.
4. Decide LONG leverage/capital. The sleeve is off until then.
5. PE2 long provisional-entry OOS re-judgment is armed only after both full-PIT
   roots extend at least 60 days past 2026-05-28 and have enough trades.
6. Build the continuous forward replay orchestrator that reruns the four frozen
   component configs into `continuous_forward_replay`.
7. Binance forward liquidation capture needs a permitted-region host. The current
   host idles harmlessly with zero Binance data.

## Current Research Direction

The full window is open for pre-registered research again, but the methodology
bar did not change: both venues, full PIT, causal features, cost/funding, and
pre-stated decision rules.

Active programs:

- **Forward data stack:** P11 taker-flow full-universe completion is idle-time;
  P12 liquidation-proxy calibration waits on a mature forward liquidation tape
  (~2026-07-10). All remaining evidence paths are forward-only: demo fills →
  R4 calibration, dynexit shadow, forward-watch leads (≥100 trades/book).

Closed same-day (2026-06-12) and not to be rescued:

- E1 composite size tilt ended at Stage-0 NO-GO (+0.15bp/trade bybit).
- P10 event-level taker-flow conditioning failed and is retired on this window.
- E2 regime family NULL — V1/V2 destroy MAR vs the live gate (pooled −1.96 /
  −2.52); the binary uptrend gate stands.
- Daily-granularity sizing-conversion on the continuous book is closed.

## Decision Rules

Forward demo/paper is the arbiter. MAR is primary, Sharpe secondary.

### Tier 1 - Investigation

- MAR delta positive on a majority of venues, or one venue positive with the
  other not badly worse.
- No return sign-flip vs control.
- At least 30 Bybit / 20 Binance trades unless explicitly labeled a tiny scout.

### Tier 2 - Demo Candidate

- Positive return on both venues.
- Pooled MAR delta > +0.1.
- Neither venue worse than MAR delta -0.5.
- Trade counts clear Tier 1.
- Fragility diagnostics reported, never used to rescue a weak cell.

### Tier 3 - Real Money

Strict and currently unmet:

- At least 30 days forward demo/paper.
- Forward MAR > 0 on both venues.
- Drawdown < 50%.
- Daily reconciliation.
- Bootstrap pooled MAR-delta left tail >= 0.
- Residual Sharpe >= +0.3.
- Stress pass and capacity >= 10x deployment size.
- No internal pre-2023 OOS exists.

## Open Methodology Debts

- **Rmom latency:** causal but knife-edge. No continuous promotion case until a
  design proves the effect can be harvested with operational margin.
- **Impact/capacity:** R4 realized-fill calibration waits on live fills and depth
  collector data.
- **Forward evidence:** continuous forward window is immature and signal replay
  orchestration is incomplete.
- **Funding/data freshness:** Binance June ancillary top-up remains blocked from
  the dev box.

## Helpers

- Reconcile: `bash scripts/reconcile.sh`
- Tier-2 robustness: `python scripts/r1_robustness.py --sweep-tag <TAG>`
- Continuous readiness: `python -m liquidity_migration continuous-forward-readiness --paper-only`
- Hedge dry-run: `.venv/bin/python scripts/run_continuous_hedge.py --venue bybit`
- Vision backfills: `scripts/backfill_binance_{funding,metrics,bookdepth}_vision.py`

## Non-Negotiables

1. Never set `REAL_MONEY=true` without explicit owner instruction.
2. Never present continuous as promoted or paper-ready.
3. Both venues matter; single-venue wins are not enough.
4. Full-PIT, causal features, ledgers, and cost modeling are correctness gates.
5. Do not loosen Tier 3 to rescue a result.
6. Pre-push gate before any push: ruff plus pytest.
7. Do not commit or push without operator confirmation.

## How To Update

Keep this file short. Research results go in `docs/research_summary.md`.
`docs/preregistration/` keeps only receipts that still bind an active
deployment, candidate, or methodology decision.
