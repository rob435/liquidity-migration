# Research Program State

Last updated: 2026-07-10.

This is the live operating page. Durable research decisions are in
`docs/research_summary.md`; dated experiment anchors are indexed in
`docs/preregistration/INDEX.md`.

## Operational headline

| Sleeve | Mode | Current state |
| --- | --- | --- |
| `continuous_ensemble_v2` | Bybit demo + paper | Base sleeve stays on; sniper is retired; safety release is pending deploy |
| `LongV11aDivWeekendVol` | Bybit demo + paper | On; one small ADA forward pair exposed timing/exit skew |

- Mainnet is not enabled. Changing that requires an explicit owner instruction
  and new evidence.
- Direct Bybit checks after the 1000TAGUSDT incident found zero positions and
  zero open orders.
- The safety runtime release rooted at `77bf04304` is deployed and independently
  verified; follow-up operator-only changes do not alter the trading object.
- First clean-window entry: TACUSDT p3 short opened at
  `2026-07-10T02:00:00Z`, 8,000 units at 0.004117. It has a paper twin, venue
  TP at 0.003647, and durable planned exit at `2026-07-11T02:00:00Z`. Quick
  reconciliation is 1/1 paired, D9-confirmed, with 65.15 bps adverse demo entry
  slippage and no unmatched/status/reason failure. Sniper remains absent.
- The Bybit depth and liquidation collectors are active and fresh. They are
  forward context/shadow data, not historical alpha evidence.
- An external liveness dead-man URL is still not provisioned. The on-box timer
  works, but an off-box heartbeat should be added before any mainnet discussion.

## 1000TAGUSDT incident

Entry equity was `$10,039.6785`. Venue executions and account records reconcile
as follows:

| Layer | Base | Sniper | Total |
| --- | ---: | ---: | ---: |
| Price PnL | -$72.44380000 | -$15.10110000 | -$87.54490000 |
| Trading fees | -$0.29927414 | -$0.05532786 | -$0.35460200 |
| Before funding | -$72.74307414 | -$15.15642786 | -$87.89950200 |
| Six funding credits | — | — | +$0.20271274 |
| Bybit account Closed-PnL | — | — | **-$87.69678926** |

The final loss was 0.873502% of entry equity. The local sniper ledger is not
venue authority because the shared-symbol exit was historically attributed to
the wrong leg. Full reconstruction and decisions:
`docs/incidents/2026-07-10-1000tag.md`.

This event does not establish that a fixed stop helps. Full portfolio replays
of 20%, 40%, and 80% adverse stops reduced MAR on both venues. What it does
establish is that a demo-only add-on without paper/backtest parity, an explicit
loss budget, or reliable component attribution was unjustified.

## Continuous target

- Baseline clock: `2026-06-18T19:54:00Z`.
- Components: p3 `1/3`, p4p3 `2/9`, p4p5 `4/9`.
- Entry: stable causal rmom q25, inverse-vol sizing (`target=0.01`, clamp `2`),
  prior-day BTC uptrend gate, and `CTRL_BTC_RISK_70_90_35` sizing.
- Portfolio: max 25 active shorts, max 5 new per cycle, BTC+ETH hedge, BTC-vol
  regime, daily rebalance off.
- Exit: component TP12 and durable 24-hour max hold.
- Disabled: sniper, fixed/server stop, left-decile, stop-approach, failed-fade,
  breakeven, re-entry cooldown, portfolio heat overlay, account drawdown overlay.

Sniper is pinned off in demo, paper, deploy, verify, and recovery. Cleanup still
handles legacy or late sniper fills while new sniper entries remain disabled.

## Deployed safety release

- Side- and component-aware WS risk reconciliation, orphan adoption, side-flip
  handling, false-empty protection, quantity-conserving Closed-PnL allocation,
  and cost-source provenance.
- Durable planned exit deadlines and restart recovery for CONTINUOUS and LONG.
- Stable-only residual momentum with exact schema, duplicate/non-finite guards,
  and no provisional rows entering signals.
- Wallet-only equity high-water persistence separated from entry-health
  snapshots, so a non-wallet snapshot defect cannot erase risk memory.
- Guarded ledger reset: dry-run default, explicit execute, flat/no-orders check,
  REAL_MONEY refusal, writer quiescence, credential binding, lock, archive hash,
  fsync, allowlist deletion, and retained high-water state.
- Reconciliation fails on stale remote market planes, separates open exposure
  from historical notional, and labels local price PnL/fees/venue allocations
  without pretending funding is present.
- LONG selected-entry rejections are durable; deterministic alerts are
  restart-safe and rate-limited by stable rejection class.
- `scripts/ops.sh` is the one operator surface for status, reconcile, equity,
  reset, research plans, tests, and checked deploy.

## Latest pre-reset reconciliation

- CONTINUOUS: paper 12, demo base 9, paired 7, paper-only 5, demo-only 2,
  sniper-only 4, open sniper 0, exit-reason divergences 1.
- LONG: paper 1, demo 1, paired 1, no unmatched entries. The ADA pair has about
  9.47 hours of entry skew and 34,091.786 seconds of exit skew.
- Unknown or failed PIT status now makes the LONG three-way leg fail rather than
  returning a green headline.

The forensic window was archived as
`data/_archive/ledger-reset-20260710T015456Z-tail-safety-20260710.tar.gz`
(SHA-256 `a4c5bf5df0338f7f320004d51e16cc932d2ceac5867e8a7fe7c36b1670e2c076`).
The immediate post-reset reconcile was 0/0 clean for both sleeves. The TAC p3
pair above then opened on the new clock; LONG remains 0/0 and CONTINUOUS is now
1/1 clean. The account is intentionally no longer flat while that tracked demo
position is open.

## Long v11a research read

Latest internal refresh through the 2026-06-23 signal day:

| Venue | Trades | Return | Max DD | Sharpe-like |
| --- | ---: | ---: | ---: | ---: |
| Bybit | 188 | +32.87% | -3.46% | 1.98 |
| Binance | 190 | +27.59% | -4.00% | 1.46 |

The object survives best-month removal, 2x/3x cost stress, worst-12-month
windows, and the matched symbol null on both venues. The material dependency is
unchanged: removing take-profit exits flips both venues negative. Treat the
small forward sample as execution evidence, not validation.

## Research and data readiness

- `continuous-tail-survival-2026-07-10.md` registers only control plus
  0.10%/0.15%/0.25% ex-ante +100%-loss budgets. No heavy run has executed.
  Signals end 2026-07-10 exclusive; exit-path kline/funding data ends 2026-07-12
  exclusive. Both venues, full stable-rmom history, exact funding cadence, and
  byte-bound full-PIT receipts are required for a positive verdict.
- `continuous-granular-adverse-risk-2026-07-10.md` registers the separate
  executable adverse-state mechanism study. No treatment run has executed.
- Current canonical roots are not granular-ready. Bybit has no canonical
  `klines_5m` dataset in the current root; Binance’s legacy granular files are
  stale before the current PIT tail. The old 2026-06-27 5m validation artifact
  is not current-root readiness.
- Strict bounded audit: Bybit 2026-07-03..09 has 4,288 PIT symbol-days; 5m is
  missing, funding is complete on 14.16%, OI has 607 partial days and no
  complete hourly days, and premium content is invalid under the new contract.
  Binance 2026-06-26..07-02 has 5,292 PIT symbol-days; legacy 5m/metrics have no
  complete current-window days, bookDepth is missing, and funding/OI/premium/
  taker-flow completeness is 83.79%/79.48%/83.79%/83.31%.
- Forward Bybit depth/liquidation capture may inform later shadow diagnostics;
  it cannot fill historical treatment features.

## Next actions

1. Let both sleeves accrue a post-fix forward sample; reconcile after meaningful
   fills or any VPS/data change.
2. On the larger machine after 2026-07-12, refresh/verify both full-PIT roots and
   run the frozen tail-survival matrix. A pass only authorizes a new shadow
   review; it does not change the live profile.
3. Build and audit granular datasets before running the adverse-state study.
   Do not infer missing sub-hour data from 1h bars.

## Canonical references

- `docs/operations.md` — operator commands.
- `docs/promoted_trading_logic.md` — active profile/runtime contract.
- `docs/research_summary.md` — durable evidence and decisions.
- `docs/data_roots.md` and `docs/pit_gate.md` — data/PIT contracts.
- `docs/preregistration/INDEX.md` — active experiments and closed arcs.
