# Continuous V2 Next-Level — Program Synthesis (mid-program)

Date: 2026-06-20
Author: Claude (operator-directed next-level research push)
Run label: `exploratory` throughout. **No real-money claim. `REAL_MONEY` stays false.**
Parent plan: `docs/preregistration/2026-06-19-continuous-v2-next-level-ab-research-plan.md`
Progress log: `docs/preregistration/2026-06-19-continuous-v2-next-level-progress-log.md`

This is the one-page decision surface for the next-level push. It exists so a serious
quant can audit the whole effort without trusting my memory or re-reading every receipt.

## What the push built (infrastructure — reusable, tested)

- **Wave 0 — both baselines frozen + reproduced** (hashes verified): `V2_LIVE_RESEARCH_CONTROL`
  (TP12, vol-off, `bfa8d385`) and `V2_EVIDENCE_ANCHOR` (TP10, vol-on max4, `6579c8ec`).
  Added a byte-identical `rebalance_rule` hook to `build_full_ledger` (live forward clock
  unchanged). Receipt: `...-phase0-baseline-construction.md`.
- **Wave 1 — full 1m PIT data, 100% coverage**: bybit 2401 + binance 2238 trade-window
  symbol-day partitions, 0 gaps, 0 checksum failures, 176 MB. Both 1m sources verified
  reachable + checksum-valid (corrected the "Binance region-blocked" assumption — that is
  FAPI-only, not the Vision CDN). Receipt: `...-1m-data-foundation-construction.md`.
- **Wave 2 X1 — 1m intrabar execution engine** (`liquidity_migration/intrabar_engine.py`):
  reuses the deployed engine's exact exit helpers so only path granularity differs; first-
  touch stop/TP, same-bar ambiguity → adverse-first, null-minute handling; plus a separate
  `resolve_dynamic_tp_1m` for trailing TP. 11 unit tests; real-data validated (100% reason
  agreement with the 1h control on both venues). Receipt: `...-intrabar-execution-engine-construction.md`.
- X2 order/fill ledger is partial (ExitResolution carries the fill); X3 cost calibration is
  GATED on VPS demo fills (not local) and registered as a dependency.

## What the mining found (5 books closed, every one a falsifier-backed both-venue NEGATIVE)

| Book | Question | Verdict | Killing falsifier |
|------|----------|---------|-------------------|
| A — stops/TPSL | Can real stops cut tails without cutting the edge? | No. Stops lose 2–5 MAR both venues, worsen drawdown. | Loses to hash-time null; mechanism = stop cuts the reversion-to-TP trades. |
| A2 — disaster stops (tail lens) | Does a wide disaster stop cap the liquidation tail? | No — inverse-vol SIZING already caps each name to ~1% of equity; price stops cap ≈0 of the tail at 0.9–4.7 MAR cost. | worst_trade-capped ≈ 0 (Binance: worse); disaster-stop need is downstream of the leverage (G) decision. |
| B — entry admission | Do causal 1m pre-entry features admit/size better? | No two-venue candidate, but the program's BEST signal: `upper_wick` 1m-exhaustion, real IC +0.10/+0.12, beats hash, fresh info. | Admission loses to diversified control; sizing beats hash but Binance-only. |
| E — dynamic TP | Does path-conditional (MFE-extension trailing) TP reconcile the venue split? | No. Pleases neither venue. | Binance loses to hash; closes exit-TP end-to-end (flat F2 + vol-scaled F2b + trailing E5). |
| G — vol-control rework | Is there a daily risk-control timing edge to recover? | No — the adjuster is a pure LEVERAGE dial. | Constant-gross controls match/beat the vol-timed arms; prior "hurts Bybit" was a TP confound. |
| F — BTC regime sizing | Does BTC-vol regime time the BOOK gross better than random? | No. Faint Bybit-only, noise-dominated. | Mean-1 + hash-permuted null: Binance loses to hash. |

## The one robust conclusion

**v2's edges are real but DIFFUSE, and the tradable residual is VENUE-SPLIT.** Across the
exit side (stops, dynamic TP, vol-control) and the entry side (1m admission/sizing) and
exposure timing (BTC regime), at full 1m path fidelity, with hash/delayed/constant-gross
falsifiers, **nothing clears the both-venue candidate bar.** The recurring mechanism is a
genuine Bybit↔Binance microstructure split:

- **Exit side:** Bybit fades revert fast-and-hard then bounce → prefer a TIGHT exit; Binance
  fades revert slow-and-far → prefer a WIDE exit. Irreconcilable with any single TP rule.
- **Entry side:** the `upper_wick` exhaustion signal is real on both venues but only TRADABLE
  (as a sizing tilt, beating its hash) on Binance.

So the two operator-gated, venue-specific leads point in OPPOSITE directions:
- **Bybit-only:** raise TP to ~12–15% (F2 lead, exit side).
- **Binance-only:** `upper_wick` / `rv_30` 1m-exhaustion sizing tilt (Book B, entry side).

Neither is a two-venue candidate; both would need a separate operator-gated no-order
forward shadow and would void the frozen forward ledger.

## Mainnet risk-control answer (disaster-stop addendum)

A disaster/price stop is NOT the missing mainnet risk control. Inverse-vol per-name
sizing already bounds each name's hit to ~0.85% of book equity even on −143%/−258% MAE
names (the blow-up-prone high-vol names get sized down to ~1% notional). Wide stops
(25–80%) cap ≈0 of that tail and cost 0.9–4.7 MAR. The liquidation tail only reappears
if gross is levered up (Book G's dial), so the real mainnet design is: cap gross +
position-level liquidation guard + correlated-squeeze-day cap — NOT a per-name profit
stop. Receipt: `...-disaster-stop-tail-construction.md`.

## Operator-directed deep dive (2026-06-20): adverse trades, TWAP entry, dynamic TP

- **What goes hard against us = high vol + big run-up** (rv_30 / run_up_120 IC ≈ −0.21 vs
  MAE, both venues; top vol decile blows up ~20% vs ~0% bottom). The TWIST: the SAME
  features have +0.17/+0.24 IC vs realized GROSS — the scary trades are the BEST trades
  (run hard against, then revert to a bigger profit). This single fact explains why stops
  fail, why inverse-vol sizing is the right tail control, and why disaster stops are
  unnecessary at native gross. Receipt: `...-adverse-trade-characterization.md`.
- **Event-driven TWAP/VWAP entry does NOT help** — it gets a WORSE average short price
  (~58% of the time) because the fade's edge is the immediate post-entry reversion;
  averaging in shorts the reversion at lower prices. Single-shot at the signal is optimal
  on price. TWAP's only real use is impact reduction on large clips (capacity/cost, gated
  on demo fills), not entry alpha. Receipt: `...-twap-entry-construction.md`.
- **Dynamic TP, pushed five ways** (flat F2 / vol-scaled F2b / trailing E5 / time-decay E6
  / run-up-conditional E2): the Bybit-tight / Binance-wide split is FUNDAMENTAL; no single
  rule reconciles it. New result: a run-up-CONDITIONAL TP (wider target on bigger-pop
  fades) is a real Binance-only refinement (+0.00149 EW, beats flat TP15 and its hash) —
  sharpens the Binance wide-TP lead. Receipt: `...-book-e2-conditional-tp-construction.md`.
- **Methodology adopted (operator direction):** perfect the trade rule on EQUAL-WEIGHT raw
  returns first; apply inverse-vol sizing only at portfolio construction. A richer
  gates-off (btc_trend_gate=off + flat sizing) research dataset is being built for more
  statistical power. Receipt: `...-research-dataset-construction.md`.

## Honest framing

- This is mining done as the operator asked: registered, ledgered, exploratory-labelled,
  with a falsifier for every apparent edge and an explanation for every venue split. The
  value delivered is mostly NEGATIVE knowledge — we now know, with 1m fidelity rather than
  1h guesswork, that the exit/stop/vol-control/regime axes do NOT hide a two-venue edge, so
  forward effort should not be spent there.
- The frozen v2 object stands unchanged. The next useful evidence is FORWARD demo/paper
  accrual on the frozen object, not more in-sample mechanism mining — consistent with the
  pre-push state before this program.

## Remaining books (not closed, with reasons)

- **C (TWAP / execution impact):** gated on X3 cost calibration from VPS demo fills (not on
  this research box). Cannot be honestly run on uncalibrated market-order fills.
- **D (rank exits / replacement):** adjacent to the already-closed F-exit-timing (shorter-
  hold / rank exits lose by cutting TP-runners); low prior, not yet run.
- **H (flow / squeeze):** Binance-only exploratory per the 2026-06-19 amendment; a two-venue
  claim needs a full-market Bybit flow archive first. Prior pass already closed Binance-only
  flow overlays + sizing.
- **I (portfolio interaction):** needs the long sleeve; cross-sleeve reservation/cluster caps
  are a portfolio-risk question, not a continuous-alpha question.

## Artifact index

Plan → progress log → this synthesis, plus per-book receipts
(`...-book-{a,b,e,f,g}-*.md`) and the Phase-0/1m/engine construction receipts. Run roots
and the 1m cache live under `backtest-runs/` and `~/SHARED_DATA/continuous_v2_1m/` (data,
not committed; cited by the committed receipts).
