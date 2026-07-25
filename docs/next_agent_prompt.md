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
  valuable finding.
- **Phase 1 — re-screen once at t ≥ 3.25.** If nothing survives, do not run more
  sweeps. Go to Phase 2.
- **Phase 2 — three parallel A/Bs.** Cross-venue replication (2A); regime
  conditioning (2B — a BTC 30-day uptrend gate took a short book from +1.29 to
  +41.09 bp/day *while improving* its tail, the most promising single lead in the
  program); and the registered basket-short structure experiment (2C, see
  `docs/preregistration/basket_short_tail_experiment_2026-07-25.md`).
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
