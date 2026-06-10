# Research Program State

**Last updated:** 2026-06-10
Live/operational state plus binding decision rules. Research conclusions live in
[docs/research_summary.md](docs/research_summary.md); the active research charter is
[docs/research_plan_alpha_hunt_2026-06-10.md](docs/research_plan_alpha_hunt_2026-06-10.md)
(§8 = the forward pipeline + Wave-1/2 outcomes).

## First Read

1. `STATE.md` — what is running and what rules bind us.
2. `docs/research_summary.md` — consolidated findings and next direction.

Old one-off receipts were consolidated and deleted (again 2026-06-10). Git history is
the archive.

## Current Status

Liquidity-migration is research-stage. **Nothing is approved for real money**
(`REAL_MONEY=false`; demo/paper only). The VPS runs ONLY the continuous system
(operator re-shape 2026-06-09); SHORT/LONG sleeves stay promoted-in-code but toggled
OFF (`deploy/sleeves.env`).

## What's Running / Wired (2026-06-10)

- **CONTINUOUS demo book — live default is now the validated winner_base 4-component
  ensemble** (`continuous_ensemble_v1`: p3 .30 / p4p3 .20 / p4p5 .40 / tp14 .10,
  frozen receipt weights, per-component age floors + venue-side TPs, w90/tv0.045/max4/
  ddh-0.04, NO momentum hurdle, rmom q25 + BTC-uptrend gate). Replaces the deprecated
  single-component `continuous_rebalance_v1` (kept resolvable for old ledgers).
  Demo fills = execution evidence, not alpha proof; the rmom latency caveat stands.
- **2f BTC+ETH hedge (banked, Tier-2 ceiling): live path wired.** `HEDGE_MODE=2f`
  default in `run_continuous_hedge.py` (per-leg plans, warm-starts carry eth_ret,
  ETH-thin fallback to single-BTC). REAL submit branch exists but is double-gated:
  `SUBMIT_HEDGE=1` + `CONFIRM_DEMO_ORDERS=1` (default off, dry-run logs daily).
- **Sniper (Tier-2 demo candidate): fully wired, default OFF.** PostOnly +8%
  quarter-size Sell limit per fresh entry with disaster stop attached; per-cycle fill
  reconcile → first-class trade rows; cancel/exit with the base. Arm with
  `CONTINUOUS_SNIPER=1` AFTER confirming ws_risk adopts a between-cycle fill (venue
  stop present; ledger row arrives at next reconcile, ≤1h).
- **Dynamic exit: forward PAPER-SHADOW only** (in-sample NULL — cross-venue mirage).
  `continuous_dynexit_shadow.jsonl`, zero order impact, pre-registered 60d/40-shadow
  forward bar (`continuous-dynexit-forward-shadow-2026-06-10.md`).
- **Shared kline data plane (2026-06-10 parallel session):** the paper shadow
  follows the demo root's flushed kline snapshot read-only (`KLINES_FOLLOW_ROOT`,
  hardened follower: prune/staleness/self-follow guard) — one WS pool per box.
- SHORT (off-box, promoted-in-code): `drop_all_4 + age300 + ff6 + btc_trend_gate=
  uptrend` (gate Tier-2 validated; rmom inactive sentinel 10.0).
- LONG (off-box, promoted-in-code): `div` + volup125 accepted candidate — NOT
  deployed; rides with the operator's 10x leverage-cap decision.
- **VPS:** Hetzner demo host (full rebuild 2026-06-09; forward Tier-3 clocks restart
  then). Local working tree is AHEAD of the VPS — a push auto-deploys.

## Operator queue — EXECUTED 2026-06-10 (operator-directed)

1. ~~Commit/push~~ DONE: `7d871e4` (ensemble+hedge+sniper+shadow wiring, repo
   cleanse) + `a461ab7` (liquidation collectors), both auto-deploying. CI deploy
   uses the possibly-stale `VPS_ED25519_FINGERPRINT` variable — failure emails the
   operator; fix the variable if the deploy mail arrives.
2. ~~Env flips~~ DONE in the units: hedge `HEDGE_MODE=2f SUBMIT_HEDGE=1
   CONFIRM_DEMO_ORDERS=1` (submit still guard+staleness-gated at runtime);
   demo `CONTINUOUS_SNIPER=1`. Verify the first hedge cycle + a sniper placement
   in the journal after deploy.
3. ~~R4 pull~~ transport fixed (ssh; no rsync needed) — finding: the VPS ledgers
   hold NO trades yet (book restarted 06-09; no entries fired). R4 calibration
   waits for fills to accrue under the ensemble.
4. ~~Data-root refresh~~ bybit_full_pit extended to 2026-06-09 (forward replay
   clock can tick); binance klines+manifest extended to 2026-05-31 (vision).
   Binance fapi ancillary June top-ups FAILED from this box (fapi unreachable
   locally — same block as the futures WS; the completeness guard refused a
   biased write). To finish: run `bash scripts/build_full_pit_binance.sh`
   stage 2 on the VPS (Hetzner reaches fapi) or any non-blocked host.
5. ~~P3 collectors~~ DONE (operator-approved): `liquidation_collector.py` +
   always-on unit, deployed. Bybit WS live-verified; check the Binance leg's
   "alive: N rows" journal line on the VPS (dev box cannot reach fstream).
6. OPEN: volup125 + long-sleeve leverage-cap decision (long sleeve is off).

## Current Research Direction

The 2023-04→2026-05 window is **SPENT** (freeze, 2026-06-09) and the 2026-06-10
charter session completed every agent-runnable direction with a pre-registered
receipt: **banked** — BTC+ETH 2f hedge (supersedes single-BTC; pooled ΔSharpe +0.146/
ΔMAR +0.56 at max4); **information PASS** — rising-OI pops fade better (no sizing
conversion survived); **nulls** — cov-sizer (book too thin), participation-cap
dominance, shrunk/basket hedges, OI tilt + down-only, dynamic exits (§4-D closed
permanently), passive-at-touch entries; **analyses** — residual attribution
(book is residual-alpha-positive vs the 6-factor model; beta is ETH-shaped),
capacity frontier (~$5M → pooled MAR ~3.8 under the trust-region cap). Details +
do-not-re-mine lists: research_summary 2026-06-10 sections; charter §8.

**New evidence comes only from:** operator actions above, forward demo/paper
accumulation, and new data layers (`binance_usdm_metrics_5m` complete —
survivorship-free 5-min OI/taker; `binance_usdm_bookdepth_1h` ingesting; taker-flow
tick stack unbuilt). Future agents: work the operator queue and forward clocks; do
NOT re-mine the window.

## Binding Decision Rules

Forward demo/paper is the arbiter. MAR primary (pooled), Sharpe secondary.

### Tier 1 - Investigation
- MAR delta positive on majority venues, or one venue positive with the other not
  badly worse. No return sign-flip vs control. ≥30 bybit / ≥20 binance trades
  (unless a labeled tiny scout).

### Tier 2 - Demo Candidate
- Positive return on both venues. Pooled MAR delta > +0.1. Neither venue worse than
  MAR delta −0.5. Trade counts clear Tier 1. Fragility diagnostics reported, not
  used to rescue weak cells.

### Tier 3 - Real Money (strict, never loosened)
- ≥30 days forward demo/paper. Forward MAR > 0 both venues. Drawdown < 50%. Daily
  reconciliation. Bootstrap pooled MAR-delta left tail ≥ 0. Residual Sharpe ≥ +0.3.
  Stress pass and capacity ≥ 10x deployment size. No internal pre-2023 OOS exists.

## Methodology Debts (open)

- **rmom latency knife-edge** (shift3-only; grid audited correct — genuine fast
  decay): no continuous promotion case until resolved; deployed SHORT unaffected.
- Impact calibration at deployed size (R4 — blocked on the fill-ledger pull).
- Continuous forward window immature (clock starts at the data-root refresh).
- Closed 2026-06-09 (receipts kept): binance funding coverage+accrual, live-vs-PIT
  age, factor day-grid.

## Helpers

- Reconcile sleeves: `bash scripts/reconcile.sh`
- Daily research cell: `scripts/volume_events_cell.sh --cell-id X --overrides ...`
- Tier-2 robustness: `python scripts/r1_robustness.py --sweep-tag <TAG>`
- Continuous readiness: `python -m liquidity_migration continuous-forward-readiness --paper-only`
- Hedge dry-run: `.venv/bin/python scripts/run_continuous_hedge.py --venue bybit`
- Vision backfills: `scripts/backfill_binance_{funding,metrics,bookdepth}_vision.py`

## Non-Negotiables

1. Never set `REAL_MONEY=true` without explicit owner instruction.
2. Never present continuous as promoted or paper-ready.
3. Both venues matter; single-venue Bybit wins are not enough.
4. Full-PIT, causal features, ledgers, and cost modeling are correctness gates.
5. Do not loosen Tier 3 to rescue a result.
6. Pre-push gate before any push: ruff plus pytest.
7. Do not commit or push without operator confirmation.

## How To Update

Keep this file short. Research results go in `docs/research_summary.md`. Keep
`docs/preregistration/` only for receipts that still bind an active deployment,
candidate, or methodology decision.
