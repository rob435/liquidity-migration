# CONTINUOUS redesign — 2026-07-26 engine experiments

Owner direction: simplify away the three ensemble components and test whether
the regime filtering can be loosened for more deployment. Nine variants were
run through the **real engine** (`run_continuous_equity_component`, untouched;
inputs transformed via a research wrapper — no runtime code or identity hashes
modified), window 2023-03-13→2026-07-17, ledger cost model (taker 5.5 + spread
2.5 + impact), settlement-exact funding, declared tp12/sl35, single cell
`turn3_pop3` unless stated. Artifacts:
`~/SHARED_DATA/bybit_full_pit/reports/continuous_redesign_2026-07-26/`.
Driver preserved in the session scratchpad; each variant dir carries
`variant_meta.json`. **Lane-1 on seen data; grades nothing.**

## 1. Every cell (component books, unhedged, mark-to-market daily stats)

| variant | change vs baseline | trades | total | maxDD | daily Sharpe | funding P&L |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| V0 | single cell `turn3_pop3`, gate >0 (baseline) | 853 | +11.67% | −3.36% | 1.88 | −3.77% |
| V1 | gate **off** | 1,475 | +10.17% | −6.97% | 0.93 | −10.19% |
| V2 | gate loosened to >−0.05 | 1,090 | +11.10% | −5.44% | 1.35 | −5.92% |
| **V3** | **funding ≥ 0 admission** | **704** | **+11.46%** | **−1.59%** | **2.18** | **−1.55%** |
| V4 | gate >−0.05 **and** funding ≥ 0 | 868 | +8.96% | −3.97% | 1.31 | −2.09% |
| V5 | looser trigger `turn2_pop2` | 1,026 | +8.74% | −3.84% | 1.27 | −3.87% |
| V6 | `turn2_pop2` + gate >−0.05 + funding ≥ 0 | 1,041 | +7.37% | −4.50% | 0.97 | −2.14% |
| V7 | `turn2_pop2` + funding ≥ 0 | 860 | +9.89% | −2.29% | 1.68 | −1.50% |
| V8 | funding ≥ **+1 bp** admission (stricter) | 545 | +7.21% | −1.70% | 1.65 | −1.02% |

Reference: the deployed 3-cell ensemble (same window, with hedge overlay)
is +15.85% / Sharpe 1.84; its three components alone are +11.67/+11.98/+13.24 —
the ensemble's uplift over any single cell is the hedge plus compounding, not
signal diversity (the cells are nested supersets of one trigger).

## 2. What the grid says

1. **The funding admission is the upgrade (V3).** Requiring the shorted name's
   last settled funding ≥ 0 — *only fade pumps whose longs are paying* — keeps
   98% of the return on 17% fewer trades, **halves max drawdown, lifts daily
   Sharpe 1.88 → 2.18**, and cuts the book's funding bill from −3.77% to
   −1.55%. Mechanism, not luck: a pump with negative funding is a crowded
   short — shorting it pays funding *and* stands in front of the squeeze
   (the financed-longs program measured that exact population from the long
   side: `docs/research_2026-07-26_financed_longs.md`). The admission curve is
   peaked at the economic boundary, not tuned: no filter 1.88 → **≥0 2.18** →
   ≥+1bp 1.65 (V8 over-filters). Zero is where "longs pay" flips, not a
   searched threshold.
2. **The BTC gate cannot be loosened — measured, three ways.** Gate off (V1):
   Sharpe 0.93, funding bill triples, drawdown doubles. Gate >−0.05 (V2):
   1.35. Loosened gate even with the funding filter (V4): 1.31. The gate is
   not a nuisance filter; it is half the strategy (consistent with the §14
   reconstruction where the ungated short book is dead). **Recommendation:
   keep the gate exactly as deployed.** The deployment scarcity it causes is a
   property of the edge — pump-fading pays in uptrends only.
3. **Looser triggers dilute (V5/V6/V7).** `turn2_pop2` adds ~20% trades and
   loses 0.2–0.6 of Sharpe in every combination. The 3×-spike/3%-pop bar is
   carrying real selection.
4. **The three-component ensemble adds nothing a single cell lacks.** The
   cells are nested (turn3_pop3 ⊇ turn4_pop3 ⊇ turn4_pop5); their "ensemble"
   re-weights the same events. One cell + the hedge is the same book with
   two fewer hand-tuned parameter sets — retiring the §9.1 grid-smoothing
   critique.

## 3. Proposed new CONTINUOUS shape

> Single component `turn3_pop3`, age ≥ 240d, rmom-low decile, **funding ≥ 0
> admission**, BTC 30d uptrend gate unchanged, tp 12% / declared sl 35%,
> inverse-vol sizing, BTC+ETH hedge overlay unchanged.

Expected (component-level evidence): ensemble-level return roughly preserved
once the hedge overlay is applied, with roughly half the component drawdown
and a daily Sharpe premium of ~+0.3 over the deployed book. Beside it, the
carry-hold sleeve (corr **−0.08** to CONTINUOUS) does the heavy lifting on
return; the two are complementary, not substitutes.

## 4. Next discriminating steps, in order

1. **Full hedged render** of the proposed shape (single cell + funding
   admission + hedge overlay) against the deployed ensemble render — the
   parity artifact a profile change needs. Requires wiring the funding
   admission through the refresh runner; the runtime producer would need the
   same check at candidate admission (the account owner already consumes
   funding for reconciliation, so the feed exists).
2. **Cooldown as the one remaining funnel knob**: the engine skip counters
   show capacity never binds (0 skips); the binding funnel constraints are
   the 24h re-entry cooldown (606 skips) and crowd-2 (292). A single
   cooldown-relaxation cell (24h → 12h) is the only loosening left untested
   that the skip data motivates.
3. **Profile change + change point** per `docs/governance.md` when 1 holds:
   revision bump (e.g. `active_tp12_sl35_fund0_v1`), five-line note, normal
   rollout. Not authorized by this document.

## 5. Caveats

Same window that validated the sl35 render (Lane-1, seen data). Nine cells,
all reported; the only adopted cell (V3) sits on an economically-pinned
boundary, and its stricter neighbor (V8) is reported as over-filtering.
Funding admission uses the cross-venue panel's last-settled rate at the signal
bar; a runtime implementation reads the venue funding feed directly. The
funding-mode label on reports remains the coarse `partial` flag
(§16.2 label fix still open). Component books shown unhedged; ensemble
comparisons at the hedged level need step 4.1's render.
