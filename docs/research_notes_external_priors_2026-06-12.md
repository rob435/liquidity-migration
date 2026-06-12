# External-priors scouting: taker-flow / squeeze-proxy signal family (2026-06-12)

**Label: EXPLORATORY / literature reconnaissance only.** No backtest, no
parameter, no evidence claim. Purpose: assemble INDEPENDENT priors (academic +
practitioner) for the taker-flow half of the liquidation/squeeze proxy before
any in-repo signal test — the program's own parking condition on W2
("multiple-testing creep on the new dataset — needs a strong independent prior
first") and the falsifier-first standard both demand priors sourced OUTSIDE
this repo's data. All sources accessed 2026-06-12.

## 1. What the literature supports (ranked by transfer credibility)

1. **Aggressive (taker) order-flow imbalance is the dominant short-horizon
   return feature in crypto futures, and the effect family is scale-invariant
   across cap tiers.** arXiv 2602.00776 ("Explainable Patterns in
   Cryptocurrency Microstructure", Feb 2026): Binance perp LOB 2022-2025,
   five symbols spanning cap rank 1→100; L1 order-flow imbalance + net order
   flow dominate SHAP rankings at 3s horizons with "largely monotone effect
   with concavity at extremes" (extreme one-sided flow ⇒ reversion), and
   feature rankings/dependence shapes are "strikingly consistent across
   assets". Transfer caveat: 3-second horizon vs our 24h fade — qualitative
   support only (taker flow carries information; alt cross-sections inherit
   the same families).
2. **Manipulated/crowded pumps revert, and their volume composition
   identifies them.** Dhawan & Putniņš, "A New Wolf in Town?" (Review of
   Finance 2023): 355 pump events, +65% average spike, sharp reversal;
   pump-day volume signatures distinguish manipulated events. Supports:
   flow-composition features measured AT pop events separate reverting from
   persistent pops.
3. **Forced-covering rallies disproportionately fully reverse.** Equity
   short-squeeze literature ("Short Squeezes After Short-Selling Attacks",
   J. Accounting Research 2024-class; Blocher et al. short-covering work:
   forced covering ≈ 30-40% of total covering). Supports the squeeze-proxy
   decomposition: price moves driven by forced flow (covering) vs new
   positioning have different post-event return profiles.
4. **Practitioner ("tribal") codification — CVD divergence/absorption.** The
   cumulative-volume-delta playbook is uniform across practitioner sources
   (Bookmap, Phemex, Kingfisher, TabTrader, Gate guides): price-up with
   flat/declining taker-buy delta = absorption/thin-liquidity move ⇒
   reversal-prone; price-up WITH expanding taker-buy delta = participation ⇒
   continuation-prone. This is the single most widely-codified intraday perp
   heuristic not yet tested in this repo.
5. **Methodology caution — OI feeds lag during cascades.** arXiv 2310.14973
   ("Reconciling Open Interest with Traded Volume in Perpetual Swaps",
   tick data across 7 venues): some venues report "wholly implausible" OI;
   others delay booking liquidation events. Consequence: any OI-leg intraday
   feature carries a latency/quality debt exactly in cascade hours; the
   taker-flow leg (exchange tick tape) does not share it. Favors
   flow-primary, OI-confirmatory designs.

## 2. Sign-prior conflict, stated before any data is touched

For SHORT-fade quality at pop events, the priors genuinely conflict on the
taker axis:

- Crowded-pump mechanism (Dhawan-Putniņš + banked in-repo Stage-1 "rising-OI
  pops fade better"): aggressive-buy-driven pops are retail/crowded ⇒ revert
  ⇒ HIGH taker-buy support ⇒ better fades.
- CVD-divergence mechanism (practitioner + 2602.00776 concavity):
  flow-UNSUPPORTED pops are absorption/thin moves ⇒ revert ⇒ LOW taker-buy
  support ⇒ better fades.

Both cannot dominate at once on the same margin. Any Stage-1 test must
therefore be pre-registered TWO-SIDED with cross-venue sign agreement as the
primary gate, and the conflict recorded ex ante (this note) so the outcome
cannot be retro-fitted to whichever story wins.

## 3. What this un-parks / enables

- The W2 parking condition ("strong independent prior first") is satisfiable
  for the TAKER-FLOW family (priors 1, 2, 4) — not yet for re-testing OI
  variants (prior 5 actually argues against OI-primary intraday designs).
- The bybit taker-flow layer (tick archive) is survivorship-FREE
  (public.bybit.com serves delisted symbols) where bybit OI was
  survivor-only — it repairs the crippled bybit arm of the original OI scout
  for the flow family.

## 4. Sources

- arXiv 2602.00776 — Explainable Patterns in Cryptocurrency Microstructure
- Dhawan & Putniņš (2023), Review of Finance 27(3) — SSRN 3670714
- "Short Squeezes After Short-Selling Attacks" (Wiley, 1475-679X.12595);
  Blocher et al., AFA short-covering
- arXiv 2310.14973 — Reconciling Open Interest with Traded Volume in
  Perpetual Swaps
- Practitioner CVD corpus: bookmap.com, phemex.com/academy, thekingfisher.io,
  tabtrader.com, gate.com (guides, accessed 2026-06-12)
