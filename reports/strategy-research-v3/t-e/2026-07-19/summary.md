# T-E — Fresh-high entry conditioning (exploratory, Lane 1)

**Status: EXPLORATORY.** Counterfactual post-processing of the spent V2 discovery
surface plus a diagnostic pass over the already-rendered T-A books. No alpha,
robustness, candidate, or promotion claim. Grid exactly as declared in
`docs/preregistration/DRAFT_strategy_research_v4_2026-07-19.md`; no cell added.

## What ran

- Input: frozen V2 barebones CONTINUOUS ledger (16,745 shorts), V3 shared
  caches, and the two T-A render books (gate-on 2,300 entries, gate-off 4,019;
  entries = sum over the three component books).
- Feature: `hours_since_high_168h`, the exact T-C definition (PIT at entry bar
  close). The full-era bucket counts and nets reproduce the draft's inspected
  table exactly (2,530 / 1,105 / 2,336 / 10,774; +8.03 / −2.22 / −13.70 /
  −12.33 % capital) — hard-checked in code. 0 entries unclassifiable.
- Declared grid: skip if hours > H for H ∈ {1, 6, 24}; sizing tilt
  (1.0 / 0.5 / 0.25 at-high / 1–6h / beyond). Fixed-capital recurrence, era
  split at 2023-02-22, salience decomposition per cell.
- Double-verification arm: same feature recomputed from PIT klines
  (render-window cache, 8,509,322 bars) for every render-book entry; all 6,319
  entries bar-aligned (`ok`); bucket tables per book × era (entry-span era
  midpoints 2024-11-20/21). Per-component tables checked — the pattern below is
  not a merged-component artifact.

## Results

**Ledger grid (baseline −20.23% net, maxDD −38.74%):**

| Cell | Net (full) | Net Δ full | Net Δ early | Net Δ late | MaxDD | Kept |
|---|---:|---:|---:|---:|---:|---:|
| skip_h1 | **+8.03%** | +28.25pp | **−1.47pp** | +29.73pp | −6.9% | 2,530 |
| skip_h6 | +5.81% | +26.03pp | **+0.22pp** | +25.81pp | −10.4% | 3,635 |
| skip_h24 | −7.89% | +12.33pp | −2.03pp | +14.36pp | −23.0% | 5,971 |
| tilt | +0.41% | +20.63pp | −0.68pp | +21.31pp | −11.8% | 16,745 |

- Salience (skip_h1, full): forgone gross +15.97% vs saved cost +35.23% +
  saved funding +8.99% — the improvement is dominated by cost/funding savings
  on a negative-mean sleeve, as the draft's mechanism story predicted.
- The kept at-high book is genuinely positive in both eras (+0.78 / +0.13
  bps/trade), and positive in every calendar year (+0.47 / +4.61 / +0.89 /
  +2.06 % capital 2021→2024). But the rule's *improvement* is late-era
  concentrated: the stale mass was net-positive in 2022 (+9.43%) and only
  turned toxic in 2023–24, so only skip_h6 clears the both-eras leg, and only
  barely (+0.22pp early).
- Composition drift checks: median censored symbol age is similar across
  buckets (~295 vs ~270 days full-era) — no obvious listing-age proxy. Funding
  overlap: the profit sits in at-high ∩ zero-funding (+10.83%); at-high ∩
  deep-neg is still negative (−1.20%, 234 trades) — freshness does not rescue
  deep-negative funding, so T-E and T-G act on distinct margins.

**Render books (double-verification arm) — the rule fails to transfer:**

| Book | at-high bps (full/late) | >24h bps (full/late) | >24h net (full) |
|---|---:|---:|---:|
| gate-on | 2.60 / 3.22 | 1.84 / 1.99 | **+15.03%** |
| gate-off | 1.90 / 2.16 | 0.35 / **−0.26** | +5.70% |

Every freshness bucket is net-positive on the gate-on book in both eras; on
the gate-off book only >24h-late is negative. The freshness *gradient* exists
in every book (at-high is the best bucket per trade and in total everywhere),
but the *skip rule* would forfeit net-positive mass on both render books:
applying skip_h6 to the books would cost ≈ −17.8pp (gate-on) and −8.9pp
(gate-off) of capital versus +26.0pp gained on the barebones ledger — opposite
sign under both gate states.

## Read

Under the program's double-verification rule, **no T-E cell advances**: the
skip family is same-signed on the barebones ledger (era leg: skip_h6 only) but
opposite-signed on both render books. Stale-entry toxicity is a property of
the ungated barebones surface — the deployed shape's admission/gating already
neutralizes it (stale entries there are net-positive). That is the reportable
finding: freshness is a real, era-stable *ranking* signal (at-high best
everywhere), not a transferable *hard filter*. It feeds T-H as a feature and
conditions the T-F interaction grid, but no forward-ledger prototype is
registered from T-E.

## Limitations

- Spent discovery surface; nothing here is out-of-sample.
- No capacity backfill: removed/down-weighted trades admit no substitutes.
- Render-book arm is a bucket diagnostic on already-rendered T-A outputs
  (per-trade economics), not a re-render; component books share capital
  semantics only within each book, so cross-component sums are read as
  per-trade diagnostics, not sleeve capital.
- Render-book window includes the label-level holdout period at the same
  already-rendered equity/trade level T-A declared; V2 label-level holdout
  data remains unread.

## Next action

No prototype. Freshness enters T-H's frozen feature set
(`hours_since_high_168h`) and T-F's declared 2-axis grid (T-E-filtered book =
skip_h6, the only both-eras-positive cell) — both already declared in the
draft.

Artifacts: `te_grid.csv`, `te_bucket_diagnostic.csv`, `te_composition_by_year.csv`,
`te_overlap_funding.csv`, `te_render_buckets.csv`, `te_trade_features.parquet`
(local; hash in `manifest.json`), manifest with grids and reproduction checks.
