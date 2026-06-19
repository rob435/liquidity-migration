# Continuous V2 Deep A/B Research — Final Verdict (2026-06-19 pass)

Date: 2026-06-19
Plan: `docs/preregistration/2026-06-18-continuous-v2-ab-research-plan.md`
Scope: CONTINUOUS demo/paper research only. No real-money claim. Forward demo/paper is the arbiter.

## Outcome in one line

**No candidate-track improvement emerged.** Every mechanism tested this pass is closed with a
falsifier-backed negative or is data-gated. The frozen v2 control was not beaten on both venues
by any single-mechanism intervention.

## What ran (this pass)

Harness/feature work (checked-in, tested — 15/15 runner tests, ruff clean, graphify updated):

- `scripts/continuous_v2_ab_research_runner.py`: added the Binance-only Problem Book C branch
  (`C0–C7_*_BINANCE_ONLY`) with `claimed_venue_scope`, a venue guard, forced exploratory/no-Tier-2
  robustness verdict; a generalized hedge-intensity overlay (A4B math preserved); value-built
  causal `flow_resid_return` + `flow_squeeze`; a Problem Book B conviction-sizing framework via the
  engine's `size_mult_lookup` hook (with a `sign` parameter and a per-symbol causal multiplier);
  and a cached `instrument_inputs` (throughput fix).

Experiments:

| Branch | Arms | Venues | Result |
| --- | --- | --- | --- |
| C0 order-flow screen | residualized flow + nulls | binance | flow real at **trade level**; `flow_resid_return` fails the null |
| C flow hedge overlays | C2 market-flow, C3 flow-squeeze, C7 hash | binance (exploratory) | **CLOSED**: C2 matched by hash; C3 inside overlay noise floor |
| B conviction sizing | B1 score-margin, B1P path-shape, B6/B6P hash | bybit+binance (candidate-track) | **CLOSED**: both beaten by their hash controls |
| C1 flow sizing (de-risk) | C1 idiosyncratic_flow, C1H hash | binance (exploratory) | **CLOSED**: MAR −3.85, drawdown ~doubled |
| Execution E1 entry-timing | sell-into-strength stop + null | bybit klines_5m (exploratory) | **CLOSED**: net −20% to −35%, adverse selection, loses to random null |
| Execution E2/E3 (maker, clip-size) | — | — | **DATA-GATED / open**: cost-axis, not timing; needs fill-prob + both-venue depth |
| Exit timing (F) | shorter-hold / time-decay / MFE-giveback + random null | bybit+binance (no-order shadow) | **CLOSED**: every rule −36% to −87%, cuts TP winners; 24h hold validated |
| Exit TP level (F2, lifecycle) | TP sweep + hold + decay + partial + vol-scaled, full lifecycle | bybit+binance | **venue split**: raise-TP robust on Bybit (MAR +1.8/+2.2) but Binance DD doubles → no both-venue candidate; vol-scaling doesn't reconcile |

## What passed / failed

- **Passed to candidate: nothing.**
- C2 market-flow hedge intensity: MAR Δ −0.44, indistinguishable from its hash control (−0.47).
- C3 flow-squeeze hedge intensity: MAR Δ +0.07, inside the overlay's ~±0.45 noise floor.
- B1 score-margin sizing: venue-split (+0.232 / −0.356), pooled −0.062, **below its hash control**
  (which scored +0.348 — a spurious sizing-mechanism artifact that even tripped the loose rule).
- B1P path-shape sizing: negative both venues (−0.42 / −0.51) despite the strongest within-symbol
  IC — the W5 "real IC, sizing doesn't harvest" lesson, reproduced under v2.
- C1 idiosyncratic-flow de-risk sizing: MAR 8.185 → 4.336, drawdown −3.27% → −5.87%.
- E1 Bybit intrabar entry-timing (exploratory): net −20% to −35% of control at every stop δ, with
  adverse selection (the missed shorts were the *better* ones) and losing to a random-bar null —
  selling a fade short into intrabar strength selects continuation-risk losers.
- A4B price/carry regime hedge intensity (prior foundation pass): mixed exploratory, not accepted.
- Exit timing (Book F, both-venue no-order shadow): every rule (shorter hold 12h/18h, time-decay,
  MFE-giveback 3%/5%) is −36% to −87% of control on both venues, barely beats a random-exit null,
  and the loss is driven by cutting 150–420 trades that ride to the +10% TP. The fixed 24h hold is
  **validated** — the 24h `max_hold` bucket looks net-negative only because the winners already left
  via TP (a selection illusion); the giveback is not causally separable from the pullback-then-TP path.
- Exit take-profit LEVEL (Book F phase 2, full lifecycle both venues): raising the component TP from
  10% to 12–15% is the single robust IMPROVEMENT found this session — but **Bybit-only**: lifecycle
  MAR +1.79/+2.23 on Bybit (return +24–31pp, Sharpe 3.71→4.1, bootstrap 90–95%) vs −3.66/−3.45 on
  Binance (drawdown doubles). A volatility-scaled TP does not reconcile it (worse on Binance). The fade
  reversion overshoots 10% on Bybit winners, but Binance non-reverters keep running against the short →
  a fundamental venue split, not a both-venue candidate.

## Exact artifacts

- Almanac (Binance, flow features): `backtest-runs/continuous_v2_feature_almanac_2026-06-19_cflow/`
- C0 screen: `backtest-runs/continuous_v2_feature_screens_2026-06-19_cflow/`
- C flow overlays: `backtest-runs/continuous_v2_ab_cflow_2026-06-19/` (+ robustness.json)
- B conviction sizing: `backtest-runs/continuous_v2_ab_bsizing_2026-06-19/` (+ robustness.json)
- C1 flow sizing: `backtest-runs/continuous_v2_ab_c1flow_2026-06-19/`
- Receipts: `2026-06-19-continuous-v2-c-flow-overlay-{construction,verdict}.md`,
  `2026-06-19-continuous-v2-b-score-sizing-construction.md` + `...-b-conviction-sizing-verdict.md`,
  `2026-06-19-continuous-v2-c1-flow-sizing-{construction,verdict}.md`.

## Status of each line

- **Candidate:** none.
- **Exploratory only / closed:** C2/C3 flow overlays, C1 flow sizing (all Binance-only exploratory,
  closed negative); the trade-level flow signal is real but untradeable via the tested interventions.
- **Closed (both-venue candidate-track):** B1 + B1P conviction sizing.
- **Closed (both-venue no-order shadow):** Book F exit-timing — early-exit rules (shorter hold,
  time-decay, MFE-giveback) destroy the edge by cutting TP winners; the fixed 24h hold is validated.
- **Robust single-venue lead (Bybit, exploratory, operator-gated):** raising the Bybit component TP
  from 10% to ~12–15% improves Bybit MAR by ~1.8–2.2 (lifecycle, bootstrap 90–95%). NOT a both-venue
  candidate (Binance drawdown doubles, MAR −3.5); vol-scaling does not reconcile it. This is the single
  strongest actionable lead of the session — a venue-specific exit policy / Bybit-only forward shadow,
  which is a separate Book G2 operator decision (the cross-venue disagreement is itself a warning).
- **Closed (Bybit exploratory):** E1 intrabar entry-timing — adverse selection (intrabar strength
  is bad news for a fade short).
- **Data-gated / open (lower urgency):** execution cost-axes — E2 (maker/post-only, needs forward
  fill-probability) and E3 (liquidity-aware clip-size, needs both-venue depth). These target
  cost/impact, not entry timing.

## The recurring lesson

Across regime, flow, and conviction-score mechanisms, the v2 fade book's signals are **real but
diffuse**: they have genuine within-symbol predictive IC, yet every intervention that tilts the
book by them (hedge timing, entry sizing) concentrates the correlated squeeze tail and lowers
risk-adjusted return, or is indistinguishable from a same-distribution hash. This reproduces the
W5/W6 conclusion under v2: a real IC is not a tradable edge for this book.

## Next required blocker (if a candidate is still wanted)

Entry-timing (E1) is now **closed by mechanism** (adverse selection — intrabar strength predicts the
fade short keeps losing), so a Binance sub-hourly OHLC backfill is **not** justified for entry timing.
The only remaining untested candidate-track ideas are execution **cost/impact** axes, not timing:
`E3` liquidity-aware clip-size (needs both-venue depth — Binance has hourly `bookdepth_1h`, Bybit
full-PIT has none) and `E2` maker/post-only (needs forward fill-probability + adverse-continuation
data). Neither can be a both-venue candidate without new depth/fill data, and both are lower-urgency.
With every signal-based mechanism, entry-timing, and exit early-cuts now closed — and the 24h hold
positively **validated** — the disciplined conclusion is that frozen v2 is at the efficient frontier
for the **both-venue** single-mechanism interventions reachable with current data; the next move is
**forward demo/paper accrual on the frozen object**, not more in-sample mining. The one genuinely
positive, robust result is **single-venue**: raising the **Bybit** take-profit to ~12–15% (MAR +1.8–2.2,
bootstrap 90–95%) — the same level destroys Binance via drawdown, so it cannot change the both-venue
frozen object. If the operator wants to act on it, the path is a **Bybit-only exploratory forward
shadow at TP 12%** (a Book G2 venue-specific-policy decision), not a frozen-object change.
A **both-venue order-book depth dataset** is the prerequisite blocker for the only remaining
candidate-track idea (execution cost/impact); acquiring it is an operator-gated data decision.
