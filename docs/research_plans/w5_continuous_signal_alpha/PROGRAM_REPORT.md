# W5 Continuous Signal Alpha Program — Final Report

**Date:** 2026-06-15 · **Status:** CONVERGED · **Run label:** research-stage, all code/receipts UNCOMMITTED
**Window:** `2023-04-01 ≤ signal_ts < 2026-05-01`, both full-PIT roots (bybit, binance).
**Control:** frozen `continuous_ensemble_v1` (turn3p3/turn4p3/turn4p5/age210tp14, weights
.30/.20/.40/.10, BTC-uptrend gate, inverse-vol size, 24h-hold-or-take-profit exit, BTC hedge).
Baseline MAR: bybit 4.748, binance 5.255.

This report consolidates the W5 search for a robust, both-venue, trade-keeping improvement to
the continuous fade book (operator bar: any ROBUST improvement that still takes trades;
Tier-3 real-money gate unchanged). It is the single auditable summary; per-stage receipts live
under `docs/preregistration/2026-06-15-w5-*` and `…2026-06-14-w5-*`.

## The one edge (deliverable)

**BTC-vol regime-hedge (Stage 8c, λ=0.5)** — modulate the frozen BTC hedge leg by a causal,
mean-1 BTC-volatility regime (trailing-30d BTC vol → trailing-250 percentile → intensity
`1+λ(2·pct−1)`; hedge MORE in turbulence) via the additive `hedge_intensity` hook. It is the
ONLY robust both-venue improvement found, but characterized honestly (Stage 8f) it is a
**MODEST, sub-period-VARIABLE tail-insurance overlay, not a smooth alpha:**
- pooled ΔMAR **+0.05–0.08** at realistic 1× hedge cost, both venues positive, λ-robust
  {0.25,0.5,0.75}, keeps all trades, gross-neutral;
- **beats the random-regime (hash) control by +0.6–0.8** — the regime signal is real, not noise;
- return-additive in every chronological sub-period; the long-BTC hedge leg costs a little
  maxDD in calm windows and pays in squeeze episodes (so MAR is not uniformly positive per
  bucket — bybit slightly DD-costly in the most recent third);
- lone fragility: thin binance cost headroom (breaks even ~1.2× hedge cost, −0.011 at 1.5×).

**Evaluate it as squeeze protection on demo/paper forward-watch, not as a smooth MAR gain.**

## Mechanism ledger (~18 distinct levers; verdicts)

| Stage | Lever | Verdict |
|---|---|---|
| 0 | candidate-tape reconstructability gate | PASS (not alpha) |
| 1 | score entry-priority (same breadth) | NULL — no within-cycle contention (mechanical no-op) |
| 7 | path-shape tercile-spread gate | NULL-as-registered (statistic noise-dominated) |
| 7b | within-symbol path-shape rank-IC | ADMISSIBLE (~0.10) but admissibility ≠ MAR |
| 5 | path-shape sizing | NULL — beaten by a symbol-identity control, worsens DD |
| 3 | mfe-giveback exit | NULL — decisively harmful (−2.29; earlier exits fight reversion) |
| 3b | hold extension 24→48h | NULL — harmful (−1.708; binance collapses) |
| 8 / 8b | BTC-vol regime-hedge (continuous / banded) | promising but sub-+0.1; banded fix failed |
| **8c** | **BTC-vol regime-hedge λ×cost grid** | **CANDIDATE / DELIVERABLE** (see above) |
| 8d / 8e | hedge-signal search (book-vol/DD/multifactor/blend) | book-DD binance-only; others venue-split/neg |
| 9 | regime book-sizing (size down in high vol) | NULL — harmful (−0.633; book PROFITS in high vol) |
| 10 / 10b | cross-sectional dispersion hedge regime | venue-split: **binance-robust / bybit-noise** (10b corrects a 10 gross bug; binance +0.293) |
| 4 | liquidity sniper (drop least-liquid decile) | **DOWNGRADED → venue-split** (see Stage 4 note) |
| 4c | sniper × regime-hedge combination | SUPERSEDED (inherits the sniper's bybit fragility) |
| 4d | sniper drop-decile robustness | FALSIFIED the sniper (k-fragile; single-seed control inadequate) |
| 2 | entry-style: decel filter / funding selection | decel NEGATIVE prior (not run); funding screen NULL |
| — | hedge instrument ETH vs BTC | NULL — ETH worse both venues |
| — | correlation-aware concurrency cap | NULL — concurrency correlates +ve with return |
| 8f | regime-hedge sub-period validation | the deliverable is modest, sub-period-variable tail insurance |
| 8g | aggregate-funding hedge regime | NULL — hedge-signal space CLOSED (BTC-vol unique / 6 signals) |
| — | take-profit exit | not a lever — the control ALREADY has a 24h-hold-or-TP exit |

## Root-cause synthesis (why everything but the hedge failed)

The fade book's edge is **diffuse**, and the book **profits when broadly deployed in
dislocations** (Stage 9 + the concurrency diagnostic: daily concurrency correlates POSITIVELY
with daily return; the best days are the most concurrent). Therefore every lever that
**selects, shrinks, prioritizes, or derisks the entry set** forgoes that diffuse profit and
fails to robustly harvest — entry priority, path-shape, liquidity, decel, funding, sizing,
regime-sizing, concurrency caps, and both exit directions. The only thing that improves
risk-adjusted return is an **overlay that keeps the whole book and hedges the squeeze tail** —
the regime-hedge. Among hedge signals, BTC-vol is the unique both-venue one (6 signals tested);
BTC is the best hedge instrument (vs ETH).

A genuine within-symbol liquidity IC exists on binance (Stage 4 screen, +0.134, p=0.001) but is
single-venue and not robustly harvestable as MAR (Stage 4d) — a forward-watch note, not a
candidate.

## Methodology note (banked lesson)

Stage 4 looked like a robust both-venue +0.407 MAR sniper and was briefly banked as "the
deliverable"; the pre-registered decile-robustness follow-up (Stage 4d) then falsified it
(venue-split; the single-seed random-drop control had huge MAR variance). Lesson (memory
`robustness-before-banking-standouts`): a result that clears a bar nothing else did is a FLAG —
vary the key free parameter and use multi-seed controls BEFORE banking a standout. The
remainder of the program applied this: five further low-prior ideas were killed by cheap
screens/diagnostics/evidence-checks with **zero wasted engine sweeps**.

## Forward-watch readiness sketch (regime-hedge) — operator-gated, PLAN ONLY

- **Config:** apply `hedge_intensity[day] = 1 + 0.5·(2·pct−1)` to the frozen BTC hedge leg,
  where `pct` = trailing-250 percentile of trailing-30d BTC realized vol (causal, mean-1). The
  `hedge_intensity` hook already exists in `continuous_rebalance.apply_rebalance_rule` /
  `continuous_forward_replay.build_full_ledger` (default None → byte-identical control).
- **Wire-up:** the live forward-replay orchestrator computes the BTC-vol regime daily and passes
  the intensity to the hedge leg; entries/exits/sizing unchanged (overlay only, all trades kept).
- **Monitoring:** (a) intensity fires up in high-BTC-vol regimes; (b) realized demo drawdown in
  squeeze episodes is reduced vs the unhedged-intensity control; (c) hedge turnover cost stays
  within model (watch binance, the thin-headroom venue).
- **Go/no-go:** forward demo evidence over a meaningful squeeze sample; promotion stays behind
  the three-tier demo-arbiter gate. **Tier-3 real-money gate UNCHANGED; do not set
  REAL_MONEY=true.** Forward-watch SETUP is operator-gated — this is a plan, not a deployment.

## Recommendation

In-sample search is complete; further grinding is diminishing returns and risks false
positives. **Recommend the operator either (a) green-light demo/paper forward-watch of the
BTC-vol regime-hedge, or (b) supply a new research direction / data source** (new signal family,
an OOS data root, or a different book). All W5 code + receipts remain uncommitted pending
operator approval.

**Addendum — bybit-primary steer (2026-06-15).** Operator: "we trade on bybit; if it's
bybit-robust and not completely losing on binance, worth it?" Reasonable — but the bar must stay
*bybit-ROBUST* (robust across the free parameter + sub-period, not bybit-positive-in-one-config,
which was the sniper trap). Applied rigorously this does NOT rescue the sniper or dispersion —
both have their robust edge on binance and are noise on bybit; the only bybit-robust signal is
the BTC-vol regime-hedge. It did, however, surface a real correction (Stage 10b): gross-corrected
dispersion is a robust **binance** hedge (+0.293, sign-flipped from the Stage 10 bug). So if a
binance sleeve also runs, a **per-venue hedge — BTC-vol on bybit + dispersion (or the
BTC-vol×dispersion stack, binance +0.368) on binance** — is a defensible forward-watch option
(each independently robust on its own venue; venue-specific, not a both-venue signal).
