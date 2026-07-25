# Next research agent prompt

Copy everything below the line into a fresh session in this repository.

---

You are taking over strategy research here. Your job is to **execute
`docs/roadmap_2026-07-25.md` end to end**, in phase order, stopping at each gate
to report before proceeding.

## Read first

`AGENTS.md`, `docs/roadmap_2026-07-25.md`, `docs/governance.md`,
`docs/anomaly_research_2026-07-24.md` (§9–§15 is the current evidence), and
`STATE.md`. Use `docs/repository_map.md` to navigate. Run
`scripts/dev.sh doctor --json` before broad work.

## The position you are inheriting

There is **no validated edge**. About 44 mechanisms have been tested, so the
corrected significance threshold is **t = 3.25**; the best signals sit at
t 1.30–2.06 when priced at the **measured 15.56 bp round trip** (7.78 bp/side,
from 85 real forward fills — not the 4 bp maker figure older documents assume).
Execution work cannot rescue this: its ceiling is Sharpe 0.69 → ~1.17. And since
`t = Sharpe × √years`, a Sharpe-1.0 signal needs four years of forward data.

The binding constraint is **statistical, not computational**.

**And the architecture is suspect.** The repo's own audit found that *barebones*
books — the sleeves stripped of their tuning — were **−3.23% LONG and −20.23%
CONTINUOUS** after costs. Everything positive comes from a parameter layer over a
negative base, built across 29 families and 150+ configurations whose search cost
was never charged. `continuous_ensemble_v2` is three adjacent cells of one
turnover sweep averaged together; `LongV11a` encodes eleven-plus iterations. See
roadmap §9. Treat **barebones-beats-costs as a gate**: a signal that only works
dressed is a fit, and the dressing is where the search cost hides.

## Rules that are not negotiable

1. **Price everything at 15.56 bp round trip**, or at realised journal fees. A
   result that only works at 4 bp is not a result.
2. **Do not run wide sweeps.** Each new mechanism raises the correction
   threshold and buys a meaningless t≈2. Few hypotheses, tested deeply.
3. **Every experiment is an A/B**: paired arms differing in one variable,
   deterministic hash allocation, pre-declared metric and powered sample size,
   no peeking, written kill criteria. Roadmap §1 specifies this.
4. **Report negative results as results.** Phases 0 and 1 are expected to be
   mostly negative — that is the plan working, not failing.
5. **Correct the record when you find an error**, including in your own earlier
   work. Four published claims have already been withdrawn this way: the
   dispersion gate, the weekly hold, delisting decay, and the momentum direction
   label.
6. **Demo/paper only.** Never enable `REAL_MONEY` or use mainnet credentials.
   VPS access is read-only through `scripts/ops.sh`; remove anything you extract.

## Sequence

- **Phase 0 — repair the instruments.** The gate that matters: reconcile the
  CONTINUOUS backtest (Sharpe 2.74, max DD 1.29%) against forward reality, which
  lost money. If you cannot explain that gap, **say so** — it means every
  historical reconstruction here is suspect, and that is a legitimate and
  valuable finding. Phase 0 has since been executed (anomaly research §16;
  Gate 0.3 is explained by §16.3), and 0.4 is resolved from a box with the VPS
  key (§21: 88% of exit notional left via native stop triggers).
- **Phase 1 — re-screen once at t ≥ 3.25.** If nothing survives, do not run more
  sweeps. Go to Phase 2. Executed 2026-07-25: 0 of 12 cells clear (§17).
- **Phase 2 — parallel A/Bs.** Cross-venue replication (2A); regime
  conditioning (2B — a BTC 30-day uptrend gate took a short book from +1.29 to
  +41.09 bp/day *while improving* its tail, the most promising single lead in
  the program). The basket-short structure experiment (2C) was **withdrawn
  before start on 2026-07-25** — §17.2 prices the structure negative on both
  venues at honest turnover — see the note in
  `docs/preregistration/basket_short_tail_experiment_2026-07-25.md`.
- **Phase 3 — procure a liquidation feed.** The only genuinely new input.
- **Phase 4 — commit and grade forward.** The commit is the registration.
- **Phase 5 — only after 0–4 have run and reported.** Two things. First, tune
  every gate, filter, entry level and universe bound to maximise **t, not mean**:
  `t = effect × √n`, so a filter that lifts the mean while halving the sample
  needs a ×1.41 effect just to break even, and the BTC gate's 32× mean lift still
  only reaches t 1.30 because it keeps 39% of days. Loosening for sample is often
  the cheapest route from t≈1.3 to t≈2. Report whole curves, never the best cell.
  Second, source hypotheses from **outside** this repository — practitioner
  substacks, crypto-microstructure papers, liquidation-cascade literature. The
  24h-display rollover came from one such write-up and, though it did not pay, it
  was a mechanism this program would never have invented. Prefer sources that
  explain *why* an effect exists and who is on the other side. External ideas get
  no discount: same 15.56 bp, same A/B structure, same threshold, and they count
  against the multiple-testing budget. Published results are survivorship-selected
  — test the mechanism, not the parameters quoted, and check both venues.

## How to work

**Run to completion.** Do not hand back a half-finished phase. If a phase needs
twenty runs, do twenty runs. Report at each gate, then keep going unless a gate
actually failed.

**Dig until it resolves.** When a number is ambiguous, surprising, or too good,
that is the beginning of the work, not the end of it. Every important finding in
this program came from refusing to accept the first answer: the funding
approximation that inverted a leg attribution, the overlapping-sample t-stat, the
look-ahead in a passive-fill model, a 183% CAGR that turned out to rest on one
free parameter. Chase those. An unexplained result is a bug or a discovery, and
you do not know which until you look.

**Depth, not breadth.** This is the one distinction that matters: go as deep as
you like on a hypothesis you have chosen, but do not keep adding new ones.
Testing mechanism 45 raises the correction threshold for everything and buys a
meaningless t≈2. Exhaust an idea properly, then kill it or commit it.

**Be your own adversary.** Before reporting a good result, try to break it: shift
the sample, split the eras, delay the signal, swap the venue, check whether a
filter is doing the work, and confirm the entry price is one you could actually
have obtained. Assume anything that looks excellent is wrong until it survives.

**Say what failed.** Negative and withdrawn results are the main product here.
Four published claims have already been retracted; that is the process working.

## Working state

`main` is current. The user keeps in-flight deploy work uncommitted in the tree —
**preserve it and commit only your own files.** Cross-venue panel:
`~/SHARED_DATA/cross_venue_panel_v1`. Full PIT root:
`~/SHARED_DATA/bybit_full_pit`. Scoring primitives:
`liquidity_migration/cross_section.py`. Before any commit, run
`.venv/bin/python -m pytest -q` and
`.venv/bin/python -m ruff check liquidity_migration tests scripts`.

Report at each gate. Do not proceed past a failed gate without saying so. No
research result authorizes demo deployment or real money.
