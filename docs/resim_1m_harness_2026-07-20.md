# 1m re-simulation harness — scope and limits (P0.1, 2026-07-20)

Module: `scripts/research_v3/resim_1m.py` · tests: `tests/test_resim_1m.py` ·
parity receipt: `reports/tail-risk-program/p01-resim-1m-2026-07-20/`.
Lane-1 research tooling; no runtime surface.

## What it is

A render-native 1m re-simulator for recorded CONTINUOUS trades, held to the
T-F standard: **before any variant is expressible it must reproduce recorded
exits exactly** (exit timestamp + reason equal, exit price rel diff ≤ 1e-12).
Two resolvers, both structurally causal (single forward pass, early return,
pre-entry minutes never read):

- `resolve_hourly_parity` — aggregates 1m minutes into the engine's 1h
  decision bars and mirrors `trade_lifecycle.on_bar` ordering exactly (stop
  precedence → TP touch at TP price on the bar end → `>=`-boundary close).
  This is the exact-reproduction mode and the only mode used for parity.
- `resolve_intrabar` — first-touch at 1m granularity: the variant surface for
  future registered work. Intrabar stop/TP ambiguity policy (taxonomy item
  14): when both touch inside one 1m bar the sub-minute order is
  unobservable → **adverse-first** (the stop fills), and every ambiguous bar
  is counted in `Resolution.ambiguous_bars` and surfaced in receipts — the
  policy is conservative and reported, never hidden.

Warm-state honesty (item 15): mae/mfe and any armed state initialize at the
recorded entry; tests prove pre-entry and post-exit perturbations cannot
change a resolution (`TestWarmStateHonesty`, `TestNoLookahead`), alongside an
independent 1h-oracle regression across seeds and real-ledger spot checks.

## Data surface

`~/SHARED_DATA/bybit_render_1m/klines_1m` (2023-03-26 → 2026-07-09, fetched
2026-07-20 for the T-A render universe with its own validation receipt).
**The program doc's original pointer to a `tick_ohlc_1m` dataset under
`bybit_full_pit` was stale — no such dataset exists on this host**; the June
2026 roots (`continuous_v2_1m`, `continuous_v2_tick`) are event-sliced
(conditioned on entries) and are not used. The June intrabar engine survives
only in git (`27f8506`); this harness rebuilds its semantics with the
parity-first bar it lacked.

## Parity result (2026-07-20 receipt)

All three recorded books walked: T-A `render_gate_on` (2,300), `render_gate_off`
(4,019), and the V2 barebones CONTINUOUS ledger (16,745). Every reproduced
trade matched with **0.0 exit-price rel diff and 0.0 mae/mfe abs diff**.
**Zero harness mismatches.** The non-reproduced remainder is enumerated, not
hidden:

- `out_of_domain_pre_window` / `no_1m_data`: barebones trades before
  2023-03-26 or on symbols absent from the render-universe 1m root.
- `out_of_domain_1m_ends_before_recorded_exit`: delisting tails where the
  venue's 1m tape stops hours before its 1h tape (e.g. PENGUSDT 2025-01-30:
  1m ends 15 h before the recorded `data_end`; same final price).
- `feed_divergence`: listing/delisting-edge paths where the venue's own 1m
  and 1h feeds disagree on a decision bar (e.g. SAHARAUSDT 2026-06-25 08:00:
  60/60 minutes present, 1m close 0.012474 vs 1h close 0.013045; GALUSDT
  delisting flatline). Mechanically attributed: a mismatch is only counted
  `feed_divergence` after bar-for-bar comparison of the aggregated 1m window
  against the raw `klines_1h` root shows the surfaces disagree; if the
  surfaces agree and the walk still misses, it is a `harness_mismatch` and
  the run fails.

## Limits

- Parity is defined against the raw venue feeds; the V2 engine additionally
  applied registered bar exclusions, so a handful of delisting-edge paths
  differ for surface reasons — they are quarantined from any variant
  analysis by construction.
- The harness resolves exits; it does not model fills/slippage of new exit
  styles (a variant expressing intrabar exits still owes an execution-cost
  model), does not redeploy freed capital, and makes no capacity claim.
- Bybit only; 2023-03-26 → 2026-07-09; render-universe symbols only. The
  never-opened holdout `[2025-01-01, 2026-07-06)` overlaps this window —
  the harness itself reads only recorded books and klines and grades
  nothing; any future variant run on holdout dates remains governed by the
  holdout rule.
- Per the closed-lines register, no per-trade exit/stop/trailing variant may
  be run through `resolve_intrabar` without a new prospective registration
  (new mechanism, new data, or new economics).
