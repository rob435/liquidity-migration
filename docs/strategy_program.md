# Strategy program — reset 2026-07-21

This is the single current authority for strategy evidence, direction, and next
work. `docs/governance.md` still owns evidence policy, `STATE.md` owns deployed
state, and code/tests own implemented behavior. Historical research is useful
only through the compact priors below; its old plans, queues, reports, and
one-off runners are retired.

Nothing in this document changes the active demo/paper profiles, authorizes a
deployment, or opens the separate real-money boundary.

## Current truth

- The active profiles remain `continuous_ensemble_v2` and
  `LongV11aDivWeekendVol`. They are demo/paper runtime configurations, not
  validated alpha claims.
- No researched replacement currently qualifies for implementation.
- The prospective runtime-parity stream, sleeve kill criteria, and paper
  passive-execution experiment remain active operational evidence surfaces.
- The account-kernel remediation in the local worktree is independent of this
  research reset and remains undeployed.

## What survived the audit

| Evidence | Decision-useful conclusion | Decision |
| --- | --- | --- |
| Strategy Overhaul V2 | About 29 families and more than 150 configurations exhausted the existing hourly entry/exit/sizing surface. Fixed-capital barebones books were approximately -3.23% LONG and -20.23% CONTINUOUS after modeled costs and funding. Full account parity was not established. | Stop tuning descendants of that surface. |
| Historical sleeve curves | Some historical curves are positive, but LONG is materially dependent on a small take-profit tail and CONTINUOUS does not have complete live-runtime reconstruction. | Keep as descriptive controls, not promotion evidence. |
| Breadth study | CONTINUOUS increased from about 6.55 to 7.30 bets per open day, but per-bet volatility was about 1,000 bp and average dependence about 0.21. A 25 bp effect would need roughly 5.6 years at that information rate. | Breadth alone is not a research direction. Fix quantization only as an execution-validity issue. |
| Young-listing lifecycle | The 2021-24 unconditional short effect reversed in 2025-26. A day-0 long was negative or flat. The required listing-week 1-minute cost data had zero symbol/date overlap with the 27,398-row event panel. | Retire calendar-age rules and the proposed T-L v2. |
| Execution cost | The first 23 measured demo fills showed positive 15-second/1-minute realized spread against our taker flow. The paper maker-first A/B is running toward 100 fills per arm. | Continue measuring execution separately; do not confuse cost improvement with alpha. |

The old reserved V2 label tape was not opened. It is not earmarked for the new
program: a descendant would inherit too much design exposure, while a genuinely
new strategy is better graded on post-commit days.

## Reset research read

All work in this section is Lane-1 exploration on already-seen local data. It
shaped the new plan and cannot grade it.

### Young listings: turnover decay was the only interesting lead

At event day 2, six rules were declared from price extension, turnover
retention, and already-settled funding before their day-2-to-day-7 outcomes were
read. Trades used actual hold-period funding, 100 bp round-trip cost, and a
listing-month block bootstrap.

| Rule | N | Mean net | Median net | 95% block CI |
| --- | ---: | ---: | ---: | ---: |
| Short every listing | 896 | -59 bp | +460 bp | -468 to +274 bp |
| Short when turnover retention is below 0.5 | 243 | +348 bp | +493 bp | +66 to +606 bp |
| Short pumped-and-decayed listings | 5 | -1,111 bp | -434 bp | -3,119 to +630 bp |
| Short crowded/decaying listings | 28 | -4,580 bp | +581 bp | -15,462 to +404 bp |
| Long pumped listings with persistent turnover | 98 | -722 bp | -1,015 bp | -1,341 to -105 bp |

The turnover-decay short was positive in aggregate but had only nine 2021-22
observations and each era-specific interval crossed zero. It is a mechanism
lead, not a candidate. Persistent-attention continuation was directly refuted.

### Mature symbols: the simple mechanism did not generalize

The same idea was then falsified on the canonical Bybit daily panel
(`2022-01-03` through `2026-07-03`, 889 symbols). Signals required 240 observed
days, at least 12 million USDT daily turnover, exact daily continuity, entry at
the next daily close, exit five days later, and at least seven days between
signals for a symbol. This screen includes price and round-trip cost but not
funding, so it is optimistic for a short strategy.

| Rule | Cost | N | Full mean | 2023-24 mean | 2025-26 mean | 95% block CI, full |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Short turnover decay | 100 bp | 5,729 | -45 bp | -158 bp | +84 bp | -181 to +86 bp |
| Short turnover decay | 200 bp | 5,729 | -145 bp | -258 bp | -16 bp | -279 to -16 bp |
| Short pumped + decayed | 100 bp | 241 | -170 bp | -774 bp | +298 bp | -916 to +475 bp |
| Long pumped + persistent | 100 bp | 5,171 | -119 bp | +71 bp | -312 bp | -320 to +86 bp |
| Long pumped + persistent | 200 bp | 5,171 | -219 bp | -29 bp | -412 bp | -422 to -11 bp |

Conclusion: price extension, listing age, and turnover retention are context,
not a standalone signal. Their pooled medians hide severe era dependence.

## Starting hypothesis, not mandated direction: Crowding Transfer

The first promising question changes the object being predicted. Instead of
asking whether a coin that pumped will continue or reverse, it asks whether
leveraged demand is moving into or out of **Bybit relative to the broader
market**. This is a place to start, not a prescribed destination. Research may
falsify it, split it into narrower mechanisms, or replace it with a more
interesting anomaly discovered in the data.

### Mechanism

1. Ask whether causal Bybit-minus-Binance premium, settled funding, or price
   basis describes local crowding better as a level, change, acceleration, or
   disagreement among measures.
2. Ask whether open-interest, taker-flow, turnover, or price transitions lead
   or lag that crowding, and whether the answer changes by regime.
3. Test whether any apparent effect survives removal of BTC/ETH beta and common
   cross-sectional moves; do not assume the correct trade is a naked short.
4. Study long and short asymmetries independently. Do not force symmetry or a
   matched pair when the data suggests only one side is interesting.
5. Treat response horizon and normalization behavior as research surfaces.
   Exchange-native disaster protection remains an account safety layer, not an
   alpha parameter to mine.

If supported, this family would differ from both active sleeves: the signal
would be a cross-venue state change, the portfolio could be beta-controlled,
and the trade would not require calendar age, a pump threshold, or broad market
direction.

### Feasibility already checked

- Bybit hourly premium, funding, index, mark, and open-interest partitions span
  `2021-01-01` through `2026-07-17`.
- Binance hourly premium and funding span late 2019 through `2026-07-17`.
- On `2026-07-16`, exact symbol names yielded 566 common premium/funding
  symbols and 579 common kline symbols.
- Bybit taker flow is available from `2023-03-29` but has gaps; Binance OI and
  taker flow are only recent (roughly late April 2026 onward). A long-history
  read can currently use premium/funding/basis, while flow/OI supports a shorter
  diagnostic unless coverage is extended.

## Proper work plan

### Research selection policy — no hardcoded performance gates

Lane-1 research has no universal Sharpe, return, trade-count, cost, era-sign,
or configuration-count hurdle. Those are properties to measure, not laws. An
anomaly is interesting when it is surprising, economically interpretable,
stable somewhere important, sharply regime-specific, useful for explaining a
known failure, or revealing of a data/execution artifact. Negative, inverted,
and conditional effects count as discoveries.

Choose follow-ups by expected information gain, mechanism plausibility,
effect-size shape, uncertainty, concentration, executable economics, and how
different the idea is from spent work. Record the judgment. Do not turn it into
a numeric pass/fail formula after the fact.

The hard boundaries are evidence physics: causal availability, honest
population/PIT scope, missingness, executable fills/costs/funding for a
performance claim, reconstructable accounting, and provenance. A violation
changes what the result can mean; it does not make the diagnostic useless.
Runtime and real-money safety boundaries remain unchanged.

### P0 — minimal causal research substrate

Build the smallest reusable panel that can answer the first questions, not
another family of bespoke report scripts or a months-long infrastructure
project.

- Exact symbol mapping with collisions and contract differences rejected.
- Decision time, source publication/availability time, a claim-appropriate
  execution delay, and no backward fill across missing venue data.
- Bybit/Binance price, mark, index, premium, settled funding, turnover, and the
  available OI/taker fields; every field carries a coverage flag.
- Manifest with Git/config/data hashes, date and population bounds, coverage by
  venue/year, and all exclusions.
- If common-population coverage or timing cannot support a proposed claim,
  narrow or relabel that claim and preserve the gap as an anomaly. A root name
  is not evidence.

Deliverables: a reusable cross-venue panel builder, focused synthetic
timing/mapping tests, and one compact manifest. Get to a first anomaly read
quickly; add fields only when a live research question requires them.

### P1 — anomaly atlas

Explore freely on already-seen data and keep an honest search log. Start with,
but do not limit the work to:

1. venue lead/lag, premium/funding/basis disagreement, and convergence paths;
2. price/OI/taker-flow divergences and transitions rather than static levels;
3. capital transfer between symbols, clusters, and venues;
4. funding-clock, time-of-week, volatility, liquidity, and market-regime
   asymmetries;
5. anomalies in what the active sleeves admit, reject, miss, or lose money on;
6. sign-inverted, time-shifted, and venue-local controls that expose artifacts;
7. unexpected data gaps, contract-lifecycle behavior, or microstructure effects
   that may be more valuable than the intended signal.

For every useful read, show the complete tested surface and enough time,
symbol, cluster, and regime decomposition to reveal instability. Put gross
next to actual or claim-appropriate stressed costs and funding. Report effect
size, uncertainty, concentration, turnover, capacity, common-factor exposure,
and missingness as continuous evidence rather than reducing them to pass/fail.

Maintain a compact anomaly catalog: observation, why it is interesting,
plausible mechanism, data touched, strongest artifact explanation, economic
shape, and the next discriminating test. Follow as many leads as remain
decision-useful; retire only duplicated plumbing and questions that no longer
teach anything.

### P2 — deepen the most informative anomalies

For leads that imply a tradable claim:

- try to disprove the proposed mechanism with timing, venue-local, sign,
  universe, and common-factor controls;
- compare sensible unhedged and hedged expressions without assuming one is
  preferred;
- replay through the account journal and venue rules when the claim reaches
  portfolio P&L;
- attribute gross, funding, fees, spread, impact, hedge P&L, residual beta,
  missed trades, and tail concentration;
- separate an unavailable live feature or optimistic cross-venue fill from a
  genuinely executable paper design.

Several anomalies may remain alive. The output is a better map of the market,
not an artificially forced winner.

### P3 — rolling forward grade

When a formulation becomes worth grading, commit its exact config and scorer
before the first new day; that commit is the registration. Append one row per
new day. Grade only post-commit decisions and keep mechanics-only days
separate. Multiple distinct formulations may accumulate their own honest
records. The existing LONG/CONTINUOUS sleeves remain controls and are not
modified to help a challenger.

Promotion requires the five-line note in `docs/governance.md`, a recorded
change point, stable paper execution, no sleeve kill-rule breach, and an
explicit replacement/migration diff. Promotion means demo only. Mainnet still
requires a separate owner instruction naming the deployment and risk boundary.

### P4 — directions remain open

Crowding Transfer is one starting family, not a gate around creativity.
Price-independent funding/premium carry, cross-sectional transfer, execution
reversion, regime-conditioned sleeve redesign, or a mechanism not anticipated
here may be better. Revisit an old family only with a new mechanism, new data,
or a corrected defect—not another threshold sweep wearing a new name. True
cross-exchange execution is a new capability and stays simulation/paper-only
until both legs, atomic failure handling, collateral fragmentation,
liquidation, transfer, and venue-outage risk are modeled and deliberately
authorized.

## Live task queue

- [x] Collapse old evidence into decision-useful priors.
- [x] Falsify simple young-listing continuation and mature turnover-decay rules.
- [x] Verify a viable long-history cross-venue premium/funding overlap.
- [ ] Build the minimal P0 causal substrate and publish its coverage map.
- [ ] Produce the first P1 anomaly atlas with the full search log.
- [ ] Deepen the highest-information anomalies and update this queue.
- [ ] Commit Lane-2 scorers only when a formulation is worth prospective
  learning; retain all resulting records.

No other strategy task list is active.
