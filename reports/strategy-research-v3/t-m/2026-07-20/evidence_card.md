# T-M — funding-extreme carry (Bybit + Binance robustness) — Lane-1 evidence card

Date: 2026-07-20. Lane-1 exploratory, on seen data. Script:
`scripts/research_v3/tm_funding_extreme_carry.py`. Hashes/params:
`manifest.json` (Bybit), `binance/manifest.json`.

## Claim and decision

Extreme-funding episodes (8h-equivalent settled rate beyond {0.15, 0.30,
0.50}%/8h, either sign) support a carry trade — position against the paying
crowd, optionally BTC-hedged — clearing the admission bar (era-stable net
>= +40 bp/trade after per-leg 45 bp costs and all funding legs). Decision:
whether any arm earns a Lane-2 config.

## Data that shaped vs graded

Same seen data shaped and graded (Lane-1): full funding tapes + 1h klines
of both full-PIT roots, entries floored at 2021-05-01. The queue asked for
the Binance inventory from 2019-09; the floor deliberately narrows that to
preserve the frozen G1/G2 grading windows unread
(`docs/preregistration/untouched_slice_provenance_2026-07-20.md`) — the
pre-2021-05 Binance funding tape remains unread by this family and the
full-span inventory can be run later with the provenance cost recorded.
Settlement cadence derived from observed spacing (never
`funding_interval_min` — known stale-label failure mode); sub-8h cadences
are genuine and common post-2024.

## Episode inventory (Bybit; deliverable 1)

84,761 episodes (2021-05-01 → 2026-07). Structure, era-split (full tables
in `inventory_summary.csv`, per-episode rows in `episodes.csv`):

- **Negative extremes dominate and explode post-2025**: at <= -0.15%/8h,
  e2122 3,552 / e2324 2,874 / e2526 28,324 episodes (509 symbols); at
  <= -0.50%/8h, e2526 alone has 11,141. The post-2025 perp market pays
  shorts persistently — the same crowded-short regime the T-L listing
  study surfaced independently.
- Positive extremes are 3-10x rarer and die faster (median durations of
  hours, not days).
- Age structure at 0.30%: extremes concentrate in 30-240d (39%) and
  >=240d (56%) symbols — NOT primarily listing-week phenomena.

## Carry P&L (deliverable 2): every arm fails the bar

12 declared arms (3 thresholds x 2 signs x persistence {1,2}), 119,977
Bybit trades. All cells in `arms_summary.csv`; components reported
separately (alt gross, alt funding, BTC gross, BTC funding, costs).

- **Hedged (the claim's shape): every arm negative in BOTH era groups**
  (pooled -14 to -68 bp; pre-2025 -9 to -68, post-2025 -3 to -75).
  The explicit BTC-leg model (45 bp RT + BTC funding transfer) costs more
  than any arm collects.
- **Why**: episodes resolve in hours (mean holds 2.5-16 h; the 14-day cap
  NEVER binds — capped_share = 0 in all arms). Collected funding is
  +12-22 bp (positive extremes) to +49-145 bp (negative extremes), but at
  negative extremes the alt price keeps falling while you collect (gross
  -27 to -87 bp — the paying crowd is directionally right short-term),
  and at positive extremes the favorable drift (+20-58 bp) plus thin
  funding cannot cover 45 bp unhedged, let alone 90 bp hedged.
- **Unhedged (diagnostic, carries full beta)**: best pooled cell +34 +/-
  48 bp (0.50%, positive extreme, P=2, n=536); pre-2025 positives (+21 to
  +51 bp) flip or shrink post-2025 in 11 of 12 arms. Nothing era-stable
  at +40 bp.
- Persistence gradient (P=2 > P=1 everywhere, both signs) is a real
  monotone diagnostic — longer-confirmed episodes carry more — but it
  tops out below costs; fitting deeper persistence would be post-hoc
  mining and was not done.

Exclusions, counted (Bybit): 159 entry-bar-missing, 377 exit-bar-missing
(includes symbols delisted mid-episode — for negative-extreme longs this
exclusion is favorable-biased, stated), 38 right-censored runs.

## Binance robustness

Same design, same floor: 41,357 episodes, 59,286 trades. The post-2025
negative-extreme explosion REPLICATES (13,637 episodes at <= -0.15%/8h in
e2526 vs ~1-2k per earlier era). The P&L verdict also replicates:
**0 era-stable hedged arms** (several pre-2025 hedged positives at
negative extremes, +47 to +86 bp, all die post-2025 — and their Bybit
twins are NEGATIVE pre-2025, so they are venue-specific besides being
era-unstable). Two Binance unhedged cells clear +40 bp in both era groups
(0.30%/+1/P=2: +149 pre n=28, +83 post n=494; 0.50%/+1/P=2: +1209 pre
n=3, +70 post n=192) but rest on tiny pre-2025 samples, carry full beta,
and FAIL the Bybit cross-venue check (Bybit same arms: +1.7/+23 pre-post
at 0.30%) — recorded as scanned, not banked. Venues are correlated
surfaces, never pooled as independent.

## Against the admission bar

- Era-stable net >= +40 bp hedged: no arm, any venue. FAIL.
- >= 5 independent bets/day at deployable gross: frequency exists
  (post-2025 negative extremes alone run ~50 episodes/day) but there is
  no deployable gross (hedged net negative everywhere). FAIL.

**T-M closes below bar — family dropped, recorded in the hypothesis
ledger.** The residual value is the inventory itself: the post-2025
negative-funding regime shift is now quantified per symbol-age bucket and
feeds the squeeze-state feature set (P2.1) as context, and the episode
tape is a reusable input for R2 governor design (state features, not a
carry trade).

## Non-conclusions

- No statement about pre-2021-05 episodes (deliberately unread).
- No statement about intraday path (MAE/MFE unmeasured — missing, not
  zero), maker execution, or cross-venue basis trades (different
  mechanism: perp-vs-perp basis, not tested here).
- The persistence gradient and the post-extreme drift alignment at
  positive extremes are diagnostics that may inform R2 state design; they
  are not tradeable results at these costs.
