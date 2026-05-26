# System Status

Updated 2026-05-26.

The liquidity-migration short strategy is in **committed paper forward testing**
on the Bybit demo account. The canonical configuration is the `promoted` profile
with `liquidity_migration_close_location_min = 0.30`.

## Funding-model correction (2026-05-22)

The perpetual-funding model over-charged funding by up to 8x: `_funding_lookup`
billed every funding row in a hold window, and 147 of 313 research-root symbols
carry intra-interval snapshot rows (e.g. hourly rows of an 8h rate). Fixed in
`008d34a` — funding is now charged once per settlement interval. The canonical
figures in this document are funding-corrected; the `strategy-tribunal` review
and 81-scenario sweep under "Research status" **predate the fix** (over-charged
funding understated return and overstated drawdown) and must be re-run before
their verdict stands.

## Audit-corrected re-baseline (2026-05-22)

A full-codebase audit corrected several engine bugs that move backtest output
(funding is now charged for the in-coverage window of trades that run past the
funding-data edge; the equity curve compounds the portfolio on a daily grid
rather than per-basket). The canonical `promoted` + close-0.30 single scenario
was re-run on the corrected engine over the full-PIT IS root
(2023-05-04 .. 2026-05-17):

- **Canonical single scenario** (`promoted` + close-0.30, threshold 0.4 / hold
  3d / stop 0.12 / TP 0.26 / cost 3.0x): 510 trades, total return 2750.38%
  (2850% pre-correction), max drawdown -14.16%, walk-forward avg split
  Sharpe 3.59, 3/3 pre-registered windows positive (train +139%, validation
  +257%, oos +239%).
- **81-scenario robustness sweep** on the corrected engine — symmetric grid
  threshold 0.3/0.4/0.5 × hold 2/3/4 d × stop 0.10/0.12/0.14 × take-profit
  0.22/0.26/0.30, cost fixed 3.0x: **79/81 scenarios promotable**, returns
  576%-2922%, drawdowns -29.5% to -12.9%. The 2 non-promotable cells are both
  at the grid edge (threshold 0.3, hold 2 d, TP 0.30 — the loosest entry +
  shortest hold) and fail the -25% promotion drawdown gate; they still earn
  positive returns (+1139% and +577%).
- `strategy-tribunal` on the canonical scenario with the sweep attached
  returns **WATCH** with no FAIL findings: report-consistency reconciles,
  promotion-gate passes, all six negative controls pass, **parameter
  sensitivity is `robust`** (45 robust same-family variants vs the gate's
  minimum of 3), parameter heatmaps and cost/funding/slippage matrices
  populated. The residual WATCHes are honest gaps: funding mode `partial`
  (trades past the funding-data edge are charged for the covered window only),
  the widest stress-matrix corner hits -29.5% DD, no demo execution-drift
  data attached, and the comparison-family is `unfiltered` (the scenario
  summary has no family column).

## 3-position re-baseline — the actual VPS config (2026-05-22)

The VPS forward test runs the canonical `promoted` profile with
`MAX_ACTIVE_SYMBOLS=3` (each trade ~33.3% of gross) — a concentrated variant
of the 5-position canonical research config. Re-running the canonical scenario
+ 81-scenario sweep at `--max-active-symbols 3` on the corrected engine:

- **Canonical cell** (threshold 0.4 / hold 3d / stop 0.12 / TP 0.26): 475
  trades, total return 14568%, max drawdown -22.66% (within the -25%
  promotion gate), avg split Sharpe 3.53, 3/3 pre-registered windows positive
  (min split +267%). The operating point is sound.
- **81-scenario sweep at 3 positions**: 46/81 promotable (vs 79/81 at 5
  positions). All 35 non-promotable cells fail on the -25% drawdown gate
  (returns span 1296%-15969%; median drawdown across the grid -24.9%, right
  at the gate). The three worst corners (loose threshold 0.3, high TP 0.30)
  hit -38% to -40.2%.
- `strategy-tribunal` returns **FAIL**, driven by `stress_matrix`: the widest
  drawdown corner (-40.24%) exceeds the -35% stress-fail threshold. The
  canonical operating point itself passes (promotion gate, all six negative
  controls, pre-registered windows 3/3 positive); the FAIL is a wide-grid
  robustness signal, not a flaw at the canonical point.

The 3-position concentration multiplies the edge (~14568% vs ~2750% at 5
positions) but narrows the parameter-robustness band: the canonical cell sits
right at the -25% gate, and the wide-grid corners blow well past it. Whether
this is acceptable is a deployment-risk choice rather than a
research-correctness question — same edge, just sized harder.

## Canonical setting — close_location_min = 0.30

As of 2026-05-21, **`0.30` is the canonical close-location setting** for research
and for the forward test (previously 0.45). It was chosen from an exploratory
closing-bar sweep on the full-PIT IS root (2023-2026):

- 0.30 vs 0.45: more trades (510 vs 448) and higher total return (2850% vs
  2212%), at the cost of deeper drawdown (-14.2% vs -11.6%) and marginally lower
  walk-forward split Sharpe (3.59 vs 3.71).

This is a trade-count / return vs drawdown choice, not a strict improvement.
Close-0.30 has now been through `strategy-tribunal` on the audit-corrected
engine at both 5 positions (WATCH, no FAIL findings, 79/81 sweep cells
promotable) and 3 positions (FAIL on the wide-grid stress-matrix corner;
canonical cell sound) — see the re-baseline sections above. The demo paper
forward test of `promoted` + close-0.30 runs on the VPS at 3 positions (see
Deployment status).

## Research status (prior baseline — close 0.45)

These figures predate the funding-model correction (see top of file) and the
audit-corrected engine re-baseline — they are kept for historical context, not
as live verdicts.

- A full point-in-time costed backtest, an 81-scenario parameter sweep, and an
  adversarial `strategy-tribunal` review returned a **WATCH** verdict with no
  FAIL findings: 3/3 pre-registered windows positive, six negative controls
  pass, 81/81 sweep scenarios promotable. This evidence is for close 0.45.
- The unconditional beta is small (-0.03 to BTC, -0.07 to the equal-weight
  universe), but this is **not** a universal market-neutrality result. The
  retraction in [docs/research_findings.md](research_findings.md) shows
  conditional beta is materially negative (~-0.45) on bear-universe days, so
  the edge is short alt-beta in the bear/sideways alt regime, not regime-
  agnostic — see that doc for the conditional split.
- Caveat: true-OOS validation on the dedicated pre-2023 Bybit and Binance roots
  showed the edge does not clearly generalise before 2023 (walk-forward split
  Sharpe ~0). The edge is IS-era / regime-conditional — see
  `signed_flow_research_verdict.md` on the research data root (not in repo).

## Deployment status

- The Bybit demo (paper) forward test runs the `promoted` profile at
  `close_location_min = 0.30` on the Singapore VPS, with the concurrent-position
  cap overridden to **3** (`MAX_ACTIVE_SYMBOLS=3`) — a concentrated variant of
  the 5-position canonical config (funding-corrected backtest drawdown -22.7%
  vs -14.2% at 5 positions; it clears the -25% promotion gate). The
  systemd unit pins `STRATEGY_PROFILE=promoted` and the runner refuses
  `SUBMIT_ORDERS=1` for any other profile, so `promoted` is the single
  order-submitting demo stack; `demo_relaxed` and the other candidates are
  shadow/dry-run only. This is a demo-only paper forward test — not
  Model-Court validated, not a real-money promotion.
- The demo cycle runs in **match-the-backtest mode** as of 2026-05-26
  (commit `78df65a`): `UNIVERSE_RANK_END=0` and `UNIVERSE_MAX_SYMBOLS=0`
  disable the live-ticker pre-filter so every active Bybit USDT-perp
  (~750 symbols) feeds into daily aggregation. The strategy's
  `universe_rank_max` then applies on the resulting daily-bar
  `liquidity_rank`, exactly the same way the backtest does. Without
  this widening the demo and backtest could pick different symbols on
  the same signal date because the rank denominator differed (observed
  2026-05-26 with DRIFTUSDT: same data, demo entered, backtest
  rejected at `rank_improvement_min=150` because the prior7 rank was
  computed within a 400-symbol vs 568-symbol universe). The validator
  (`_validate_demo_config` / `_required_universe_rank_end`) accepts
  `0/0` as the explicit unlimited-universe opt-in; partial misconfigs
  (one zero, one positive) still trip the universe-too-narrow guard.
  To revert to the legacy narrow-universe demo (top-400 by ticker
  turnover, smaller kline store, but demo ≠ backtest), set both env
  vars to 400 in the systemd unit and rebuild.
- No real-money trading is active: demo is the default, and `demo=False` is
  refused unless real-money mode is deliberately armed (see below).

## Execution-path resilience (2026-05-26)

The demo loop is WS-first with REST as the safety net at every layer. Recent
hardening pass shipped these crash-/drift-durability invariants — none of them
change strategy or backtest output, but each closes a specific way the live
ledger could diverge from Bybit:

- **Preflight order rows.** Every `place_order` that submits to Bybit (both
  cycles, both sleeves, including the wsrisk reduce-only exit path and the
  limit-chase fallback-market) flushes a `status="submitted",
  submit_mode="preflight"` row to the orders parquet *before* the venue call.
  If the cycle dies between submission and the end-of-cycle ledger flush, the
  `orderLinkId` is already on disk and the next cycle's
  `_reconcile_pending_order_fills` adopts the actual fill from
  `get_trade_history`. Without this, a crash-window fill would orphan on the
  venue and the ledger would carry no trail.
- **Orphan-close PnL backfill.** When `_risk_reconcile_missing_positions`
  detects a Bybit position that has vanished but the ledger still says open,
  it queries `get_closed_pnl` (filtered by symbol + close-side +
  created-after-entry) and backfills `exit_price`, `gross_trade_return`,
  `net_return`, `exit_ts_ms`, and `exit_order_id` from the actual close,
  stamped with `submit_mode="orphan_reconciled"`. Falls back silently to the
  legacy zero-PnL row on any failure — backfill can never block the close.
- **Orphan-reconciler API-failure guard.** `_risk_reconcile_missing_positions`
  takes a `position_error` argument and bails out (no orphan-closes) when the
  upstream `get_positions` failed. Before this, a single transient REST
  failure made `position_by_symbol={}` and false-positive orphan-closed every
  open trade. The main demo cycle's `_reconcile_open_trades` already had this
  guard; the wsrisk path now matches it.
- **Cache schema-drift safety.** `PrivateStateCache` (positions/orders/wallet)
  and `TickerCache` now only bump `last_event_monotonic` when at least one row
  in a WS message was applied successfully. If a Bybit schema change causes
  every row to drop, the cache goes stale on its existing timer and the cycle
  falls back to REST — preventing the "silently-stuck cache, cycle reads
  forever-fresh state" failure mode.
- **Ticker-stream startup recovery.** A REST seed failure used to permanently
  disable the WS ticker feed (the stream is skipped when the cache is empty
  and never retried). The reconcile loop now retries
  `_open_ticker_stream()` after each successful re-seed, so the daemon
  recovers automatically instead of REST-falling-back for its full lifetime.
- **Kline-warmer alert.** The hourly kline cache warmer now tracks consecutive
  failures and sends a one-shot telegram when the streak hits 3, so a
  sustained outage is operator-visible before cycles start REST-bursting on
  every bar close. Streak resets on the first success, alert rearms.
- **Ledger write ordering.** End-of-cycle parquet writes flush the orders
  dataset BEFORE the trades dataset (both cycles, both sleeves). A crash
  between the two leaves the order ledger ahead of the trade ledger so the
  next-cycle `_reconcile_pending_order_fills` adopts the order and
  re-applies the trade-close. The reverse ordering would leave the trade
  marked closed with the order detail (fill price, order_id) permanently
  missing.
- **Long sleeve ticker-stream recovery.** The long-native daemon's
  `_reconcile_loop` now mirrors the short daemon's recovery-open: if the
  ticker stream is unset and the cache has populated symbols, the loop
  retries `_open_ticker_stream()`. Without this, a single REST seed failure
  at startup would permanently disable the long sleeve's WS ticker feed.

These are mechanical / engineering hardening — none touch the signal, the
universe, or the parameters. Backtests and the `promoted` profile are
unchanged. The relevant test suites (`tests/test_liquidity_migration_event_demo.py`,
`tests/test_liquidity_migration_ws_state_cache.py`) cover each contract.

## Real-money path (built, demo by default)

A real-money (mainnet) execution path exists in the code. Which account the
private clients use is a plain `.env` toggle read by
`bybit.resolve_private_credentials()`:

- `REAL_MONEY=true` — mainnet keys (`BYBIT_REAL_API_KEY` /
  `BYBIT_REAL_API_SECRET`), real-money endpoint.
- `DEMO=true` or unset — demo keys (`BYBIT_DEMO_API_KEY` /
  `BYBIT_DEMO_API_SECRET`), demo endpoint.

Demo is the default: with neither flag set the clients stay on the demo
account, so the VPS demo is unaffected. `DEMO` and `REAL_MONEY` are mutually
exclusive — setting both true raises.

**The strategy is NOT validated for real money** — the tribunal verdict is
WATCH (not PASS), the edge is IS-era / regime-conditional, and there is no
live-fill track record. The repository ships with the toggle on demo.
