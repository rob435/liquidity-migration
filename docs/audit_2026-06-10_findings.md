# Deep Audit 2026-06-10 — Findings Record (NO fixes applied)

**Status: interrupted by operator (usage conservation). This file documents what was
found; nothing was changed.** Working tree at audit time: clean @ `151c685`.
Baseline gate: **ruff clean, 1491/1491 tests pass** (42s).

Method: 16 read-only subsystem/cross-cutting finder agents + a 59-doc staleness
audit. The adversarial verification layer was cut to save usage, so every bug
finding below is a **single-finder claim, unverified** — re-check each at the
cited line before fixing. 3 of 16 finders completed before the stop:
**hedge-sniper**, **long-sleeve**, **ws-data (bybit/binance/ws caches/kline pool)**.

**NOT covered (finders interrupted — absence of findings ≠ clean):** ws_risk.py
(full pass), event_demo.py + daemon, event-cycle modules, continuous_demo/rebalance
core, storage/ingestion, volume_events backtest core, signal harness/risk model,
reconciliation, cli/config, deploy-infra (full pass), the dedicated concurrency +
financial-math + live-scripts sweeps.

---

## Bug findings (25) — by severity

### CRITICAL

1. **`get_positions` returns only Bybit's default 20-row first page** —
   `liquidity_migration/bybit.py:505`. No `limit`, no `nextPageCursor` follow
   (pybit does not auto-paginate; `get_closed_pnl:549` and
   `get_funding_settlements:599` in the same file DO follow the cursor). With >20
   open positions on the shared netted demo account — continuous `max_active=25`
   (`continuous_demo.py:66`) + BTC hedge alone can exceed 20 — ws_risk (the single
   reconcile authority) bootstraps from a truncated snapshot (`ws_risk.py:741`):
   positions 21+ are invisible → wrongly treated as closed/untracked.
   **Fix:** paginate with `limit=200` + cursor loop, mirroring `get_closed_pnl`.
   **Must land before any sleeve redeploy alongside continuous, and before sniper
   adds resting orders.**

### HIGH

2. **`get_open_orders` same single-page truncation** — `bybit.py:466` (default 20,
   max 50/page). >20 resting orders (25-name continuous book entries, future sniper
   limits, short-sleeve limit-chase exits) → truncated snapshot → pending-order
   reconciliation wrongly terminalizes / resubmits (duplicate-order risk);
   `PrivateStateCache.replace_with_rest_snapshot` evicts cached orders 21+
   (`ws_state_cache.py:184-188`). **Fix:** same cursor pagination.

3. **Hedge runner ignores the existing hedge position: `current_hedge_qty`
   hardcoded `0.0`** — `scripts/run_continuous_hedge.py:110`. The hedge ledger root
   is parsed but never read; no venue position query. Once `SUBMIT_HEDGE` is armed:
   full-size Buy planned EVERY day at 00:35 (position doubles daily); the
   reduce/`reduce_only` path is unreachable (beta flip to ≥0 can never trim); the
   dry-run telemetry the operator is supposed to verify prints
   `current_notional_usdt=0` + full-size qty, i.e. it misrepresents the resize.
   **Fix before arming:** read the hedge ledger (or positions API) for current qty.

4. **Hedge `gross_short_frac` scaling is dead** — `scripts/run_continuous_hedge.py:59`.
   `_live_book_state` sums `notional_weight`, a column the live continuous demo
   ledger does not have (it carries `notional_usdt`/`equity_usdt`;
   `continuous_demo.py:1945-1960`, weight reconstructed on the fly at `:1480`).
   Guard never passes → gross stays 0.0 → fallback 0.5 → `target_scale` ALWAYS 1.0.
   The documented "half-deployed book gets a half-sized hedge"
   (`continuous_hedge_manager.py:128-130`) never engages; dry-run prints a constant
   `gross_short_frac=0.5`. **Fix:** compute gross from
   `notional_usdt/equity_usdt`; distinguish flat book (0.0) from unknown.

5. **Windowed long backtest force-closes end-of-window positions at the FULL
   root's last bar** — `liquidity_migration/long_native.py:1811`. `dates_all` is
   end_date-filtered but `bars_by_symbol`/`funding_lookup` are not; positions open
   at window end close at the symbol's last bar in the whole root, booking
   months/years of out-of-window drift + funding. Hits every `--start/--end`
   sub-window run (`scripts/long_native_sweep_fc_min_day.py`, robustness windows).
   Research-validity for windowed long results. **Fix:** clamp force-close to the
   last bar ≤ window end.

### MEDIUM

6. **Post-rebuild adoption breaks the hedge never-force-exit contract** —
   `ws_risk.py:1780` (recovered-path rows at `:1755-1785`). After a data-wipe
   rebuild (2026-06-09 precedent) the BTC hedge long is untracked on the venue;
   adoption decodes `lm-en-ca-*` → sleeve `continuous_addon`, then builds the row
   with `adopt_stop_loss_pct=0.12`, `adopt_take_profit_pct=0.21`, 3-day
   `planned_exit_ts_ms` → `plan_risk_exits` force-closes the hedge within 3 days
   and `repair_exchange_stops` puts a server-side stop on it. **Fix:** in the
   adoption path, special-case the hedge link/symbol+sleeve to rebuild a conforming
   tracked-only row (stop/tp/planned-exit = 0).

7. **`deploy_vps_live.sh` never enables/verifies `continuous-hedge.timer`** —
   `scripts/deploy_vps_live.sh:164` area. Unit files are copied (`:140-142`) but the
   enable block (`:154-171`), verify section (`:~222-241`), `deploy/lib_sleeves.sh`
   rosters and the liveness watchdog all omit it. The currently-running timer was
   hand-enabled; the next rebuild/deploy lands the file but never fires — the WP3
   dry-run evidence stream dies silently. **Fix:** add the timer to the roster +
   enable + verify, gated on the continuous sleeve toggle.

8. **ws_risk start-guard omits the hedge ledger** —
   `scripts/run_bybit_demo_ws_risk_engine.sh:41`. The refuse-to-start guard for
   `EXIT_UNTRACKED_POSITIONS=1` checks `LONG_DATA_ROOT` and `CONTINUOUS_DATA_ROOT`
   but not the new `CONTINUOUS_ADDON_DATA_ROOT` (added at `:30-34` of the same
   script). If unset, a live hedge BTC long looks untracked → adopted-with-stops
   (finding 6) or flattened. **Fix:** extend the guard.

9. **Hedge runner's REAL_MONEY gate weaker than the credential resolver** —
   `scripts/run_continuous_hedge.py:130`. Blocks only the literal string `"true"`;
   `bybit._env_flag` (`bybit.py:57-59`) accepts `{1,true,yes,on}` case-insensitive
   — so `REAL_MONEY=1` selects mainnet keys but does NOT trip this guard. Stub
   today; load-bearing the moment submit lands. **Fix:** use the existing shared
   gate `bybit.validate_order_submit_allowed`.

10. **Beta window frozen at the shipped warm-start (last row 2026-05-23), no live
    extension, no staleness guard** — `scripts/run_continuous_hedge.py:65`.
    `live_unit_by_day` always `{}`; docstring claims live-day extension
    (`continuous_hedge_manager.py:13-16`). Drifts one day staler per day, unbounded;
    no `warmstart_age_days` diagnostic. Also weakens the dry-run-verification value
    (near-constant daily output). **Fix:** wire the daily book-return feed or at
    minimum emit + alarm on warm-start age.

11. **`build_long_features` is calendar gap-blind throughout (BAC-1 class
    recurrence) in the PROMOTED v11a path** — `long_native.py:587` etc. The
    2026-06-03 fix converted 43 sites in `volume_events_features.py` to
    `calendar_shift/_cal_roll`; `long_native` was never converted: realized_vol
    (sizing!), return_5d/30d/60d shifts, rolling extremes, vol7/14d, atr_14d,
    quantiles, avg_rank_30d, 90d hi/lo, BTC regime SMA/RV — all row-indexed over a
    panel that drops <20-hourly-bar days. **Fix:** same `_cal_roll` conversion;
    gate by numerical-equivalence tolerance where windows are gap-free.

12. **Long 10x multiplier × volup125: worst-case IM 125% of equity (documentation
    of the known flagged interaction)** — `long_native_event_demo.py:136`
    (`notional_multiplier=10.0`), `:347-359` (→100% equity notional/position),
    `:269` (`vol_target_max_scale=1.25` → 125%), `entry_leverage=10` → IM 12.5%
    per position × 10 slots = 125% of equity from the long sleeve alone on the
    SHARED netted account; the cross-sleeve clamp is a documented no-op by default.
    This is the precise location of the interaction STATE.md says must be resolved
    before the long sleeve is re-enabled.

13. **B.3 per-symbol concentration cap applied BEFORE the vol-target scalar** —
    `long_native.py:1778` (cap at `:1770-1773`). Under volup125 (scale > 1 now
    legal) a calm-BTC regime exceeds `max_per_symbol_weight` by up to 25% whenever
    the cap binds (doesn't bind at promoted v11a numbers — latent, but violates the
    documented invariant). **Fix:** apply the cap after scaling.

14. **`BybitTradeRouter.cancel_order` WS path always raises TypeError** —
    `bybit.py:988`. Passes `orderLinkId=`; `BybitWebSocketTradeClient.cancel_order`
    (`:1183`) declares keyword-only `order_link_id`, no `**kwargs`. Swallowed at
    `:1059-1062` as `_RouterWsFailed('exception')` → WS cancel permanently dead
    (silent REST fallback in `ws_then_rest`; broken outright in strict `ws` mode)
    + guaranteed false `ws_exceptions` telemetry per cancel. **Fix:** rename the
    kwarg.

15. **Kline pool watchdog holds the pool lock across blocking reconnect I/O** —
    `bybit.py:1739` (`check_stale_connections`, lock at `:1707`). Per stale
    connection while locked: 3s thread-join + synchronous pybit `WebSocket()`
    construction (internal retry loop) + resubscribe. A network blip stales all ~4
    connections (673-symbol universe) → lock held 30s+ exactly when degraded;
    contenders blocked. (Past audit class #1.) **Fix:** mark/collect stale conns
    under lock, reconnect outside it.

### LOW

16. **`HedgeDecision.n_obs` counts the full series, not the 90d window** —
    `continuous_hedge_manager.py:139` vs `continuous_rebalance.py:331-337`
    (`beta_min_obs=60` enforced in-window). Telemetry can claim min-obs health the
    estimate doesn't have (warm-start prints 160 while ≤90 used; beta silently 0
    case reads healthy).

17. **`test_link_id_namespace` is vacuous** —
    `tests/test_continuous_hedge_manager.py:111`. `A and B or lid` is always
    truthy → the lm-en-ca-* namespace contract (ws_risk sleeve routing,
    `order_link_id.py:96-98`) is pinned by a test that cannot fail. Fix:
    parenthesize into two asserts.

18. **Live FC candidate selection truncates alphabetically; backtest ranks by pump
    size** — `long_native_event_demo.py:1045` vs `long_native.py:1647-1650`.
    On >5-candidate days live keeps the alphabetically-first 5 (60s cycles bound
    the impact). Backtest–live selection divergence.

19. **`LongNativeConfig.sizing` is dead config** — `long_native.py:307`.
    `sizing="equal"` silently runs vol-parity (`:1751-1754`); report metadata
    records the lie.

20. **Sniper fall-through books a knowable-only-at-deadline decision at the hour-1
    price when the deadline bar is missing** — `long_native.py:1736-1741`
    (live does it right: `long_native_event_demo.py:1004-1005`). Favors the
    backtest on continued pumps across kline holes.

21. **Backtest exit scan skips real hourly bars on panel-dropped calendar days** —
    `long_native.py:1554-1556` (gap-blindness, exit path): stop/TP hits during
    venue-outage days never checked.

22. **`Avg split sharpe` always renders 0.00** — `long_native.py:2069` reads a key
    `_evaluate_promotion` (`:1933-1976`) never emits, in the promotion-gate section
    of the audit artifact.

23. **`TickerCache.seed` has no empty-fetch protection** — `ws_state_cache.py:488`.
    An empty-but-retCode-0 `get_tickers` wipes every symbol and stamps the cache
    fresh (the exact class PrivateStateCache guards at `:164-182`). Contained by
    the `if snap:` REST fallback at `event_demo.py:2292`.

24. **`BinanceUSDMData._get` retries definite venue rejects through full backoff**
    — `binance.py:187` (catches its own `BinanceDataError` from `:184-185`).
    Contradicts the EXC-3 fix on the Bybit side (`bybit.py:369-374`, `:720-724`).

25. **`PrivateStateCache` REST reconcile can roll back newer WS state** —
    `ws_state_cache.py:192` + `event_demo.py:2317-2323`. A fill landing in the
    fetch→seed gap is overwritten by older REST data (phantom working order +
    missing position, seeded fresh) until the next push/reconcile (~60s). Fix
    direction: compare per-row `updatedTime` on seed.

### Suggested priority order

1. (#1, #2) Bybit pagination — precondition for sniper resting orders, >20-position
   books, any sleeve redeploy.
2. (#3, #4, #9, #10, #16) Hedge runner correctness — ALL before `SUBMIT_HEDGE=1`.
3. (#6, #8, #7) Hedge tracked-contract + deploy wiring — before the next
   rebuild/deploy.
4. (#5, #11, #13, #19–22) Long-sleeve research-validity cluster — before the next
   windowed long run / before re-enabling the long sleeve (with #12).
5. (#14, #15, #17, #18, #23–25) Hygiene tier.

---

## Docs audit (complete — 59 docs: 13 keep / 36 update / 10 fold-delete)

Single-pass agent verdicts; removal cross-check layer was cut (review before
deleting). Full machine-readable detail (every stale claim with line + suggested
replacement text) is preserved at the end of this section.

### Fold-then-delete (9) + delete (1)

All are closed/dead arcs per the STATE.md prereg policy ("keep only receipts that
still bind"). **Four carry verdicts recorded NOWHERE else — fold into
`docs/research_summary.md` (new section "Downtrend-sleeve + sniper program,
2026-06-09") BEFORE deleting, and update STATE.md's dangling receipt pointers:**

| Doc | Why | Verdict recorded elsewhere? |
|---|---|---|
| `docs/research_plan_continuous_regime_2026-06-09.md` | WP1a NO-GO / WP2 done / WP3 banked | yes — research_summary ~517-584 |
| `docs/research_plan_downtrend_sleeve_2026-06-09.md` | program terminal state | **NO — fold first** |
| `docs/preregistration/continuous-rs-squeeze-probe-2026-06-09.md` | WP1a NO-GO receipt | yes — research_summary 541-551 |
| `docs/preregistration/ridge-combiner-2026-06-09.md` | Tier-1 REJECT receipt | yes — research_summary 861-879 |
| `docs/preregistration/downtrend-opportunity-map-2026-06-09.md` | D1, arc closed | **NO — fold first** |
| `docs/preregistration/downtrend-reversal-ls-2026-06-09.md` | D2 FAIL | **NO — fold first** |
| `docs/preregistration/downtrend-bounce-long-2026-06-09.md` | D3 FAIL; "hedge+cash is final" close-out must be preserved | partial (STATE only) — fold first |
| `docs/preregistration/sniper-conditional-walkforward-2026-06-09.md` | closed sub-question (P0 stands; active candidate lives in sniper-staged-entries) | **NO — fold first** |
| `docs/preregistration/wp4-rmom-standalone-ls-2026-06-09.md` | WP4 Stage-A FAIL | **NO — fold first** |
| `docs/preregistration/exploratory/` | empty untracked dir, vestigial | n/a — rmdir |

### Keep as-is (13)

CLAUDE.md; docs/backtesting_errors_we_never_repeat.md; docs/pit_gate.md;
prereg: _template, pit-membership-trading-day-fix, continuous-funding-debt-closure,
continuous-demote-downtrend-extension, continuous-hedge-overlay,
continuous-walkforward-allocator; skills (claude+codex): research-report,
run-strategy.

### Update (36) — headline staleness themes

- **The 2026-06-09 re-shape is not reflected outside STATE.md**: README (says
  short+long run on the VPS; "nothing is promoted" contradicts promoted.py),
  AGENTS.md ("promoted profile runs on demo"), docs/data_roots.md (2026-05-22
  forward clock is dead — clocks restarted 2026-06-09; VPS root list missing 5 of 7
  per-sleeve roots; "ledgers unaffected" note now wrong — history was lost).
- **STATE.md itself**: 4 touch-ups — Last-updated says 06-09 with 06-10 content;
  sniper wiring line predates the merged `plan_continuous_sniper_orders` scaffold
  (151c685); "(8 tests)" is now 9; the rebuild-provisioning parenthetical states
  the superseded sleeve set (reads wrong in isolation).
- **Runbooks**: docs/event_demo_daemon.md (7 claims) and deploy/systemd/README.md
  (9 claims) lag the continuous-only live set + hedge timer.
- **Receipts (12 files, 1-3 claims each)**: mostly forward-clock dates, sleeve-set
  references, and pointers to since-renamed helpers.
- **Skills**: 6 `.claude/skills/*` and 6 `.codex/skills/*` SKILL.md files carry
  stale live-set/clock claims (codex ones are Codex-owned — flag, don't edit, per
  AGENTS.md). graphify-out/GRAPH_REPORT.md missing the new modules
  (continuous_hedge_manager, continuous_forward_replay, ridge_combiner) — run
  `graphify update .`.
- docs/research_summary.md: 10 operational-claim touch-ups (live-state phrasing,
  one dead script reference class); research content NOT in question.

### Where the full docs detail lives

The complete per-claim JSON (claim → line → reality → exact replacement text for
all 36 update docs) was produced by the audit run. It is preserved verbatim at:

`/private/tmp/claude-501/-Users-jhbvdnsbkvnsd-Desktop-liquidity-migration/2d10bab3-d0cb-4898-bcb6-ae43c7a249f7/tasks/wlngmdz36.output`

(tmp — copy it somewhere durable if you want it past a reboot; see "Resume
pointers" below to regenerate at near-zero cost from cache.)

---

## Resume pointers (for a future session)

- Bug-audit workflow script:
  `~/.claude/projects/-Users-jhbvdnsbkvnsd-Desktop-liquidity-migration/2d10bab3-d0cb-4898-bcb6-ae43c7a249f7/workflows/scripts/deep-bug-audit-wf_fa1ad3ad-4c0.js`
  (resume id `wf_fa1ad3ad-4c0`; only the 3 finders named above are journal-cached —
  the other 13 re-run live).
- Docs-audit workflow script: same dir, `docs-staleness-audit-wf_c8e46739-458.js`
  (resume id `wf_c8e46739-458`; ALL 9 group agents cached → re-running returns the
  full per-claim JSON at near-zero cost).
- Baseline at audit time: clean tree @ `151c685`, ruff clean, 1491 tests green.
