# Decisions

## 2026-04-03

- Chose Bybit V5 as the concrete exchange target because the spec aligns with Bybit topic semantics and closed-candle `confirm` events.
- Kept the repo flat. There is no extra package layering because the project is small and the dataflow is direct.
- Recompute cross-sectional rankings on every closed candle update instead of trying to process only the changed ticker. Ranking is inherently cross-sectional, so partial updates would be wrong.
- Add a short cycle settle delay before each processing pass so the engine ranks against a coherent candle-close wave instead of a half-updated snapshot.
- Pass candle timestamps through the queue into the confirmed signal path so close-confirmed logs and cooldown use market-event time; keep emerging alerts on wall-clock detection time because they fire before the candle is final.
- Treat BTC regime as a threshold modulator only. Regime score `0` removes the composite-score floor because the configured threshold map returns `None`; rank, Hurst, and dominance still apply.
- Use a BTC-vs-alt-basket relative-strength proxy as the dominance rotation signal. Real BTC dominance history is outside Bybit public data.
- Log every evaluated ticker to SQLite on each cycle, including cooldown-suppressed rows with `alerted=0`.
- Batch SQLite inserts per cycle and store `price`, `rank`, and `dom_falling` so the logs are useful for analysis instead of just audit trivia.
- Only update cooldown state after a notifier send actually succeeds.
- Keep a non-trivial minimum volatility floor (`1e-5`) because the raw-price momentum formula otherwise over-rewards ultra-smooth drift series.
- Add a replay harness that drives the production engine over recent historical candles rather than inventing a separate backtest-only code path.
- Add a paginated universe validator against Bybit instruments metadata because the manual symbol list will drift over time and the endpoint now exceeds the default 500-row page.
- Add a lightweight SQLite report CLI instead of expecting operators to inspect signal quality with raw SQL after every replay or live run.
- Add a one-command smoke runner so external validation is repeatable instead of depending on a manual validator -> replay -> report sequence.
- Add a benchmark CLI over the real engine path so the 100-ticker latency target can be measured repeatedly instead of asserted by guesswork.
- Add bounded-runtime support and runtime counters to `main.py` so live soak runs are repeatable and produce a shutdown summary instead of depending on manual observation.
- Add an explicit soak-run guide and production env template because deployment mistakes are more likely than code defects at this stage.
- Keep confirmed 15m history and provisional intrabar prices as separate state paths. Early watchlist logic is useful, but corrupting the confirmed history with partial candles would invalidate the close-confirmed signal.
- Use the same cross-sectional engine for both `emerging` and `confirmed` stages instead of inventing a second ranking model. The difference is the price input and alert gating, not a separate math stack.
- Do not treat every intrabar top-ranked name as a real emerging breakout. Intrabar candidates now progress through `watchlist` first and only promote to `emerging` after repeated observations show improving rank and rising composite score.
- Gate intrabar alerts on state transitions (`neutral -> watchlist`, `watchlist -> emerging`) plus separate cooldowns so the live system stays event-driven without spamming the same ticker on every Bybit kline push.
- Store both `stage` and `signal_kind` in SQLite because once the engine has intrabar state transitions, a single undifferentiated signal log becomes analytically useless.
- Treat confirmed-bar persistence as a confidence upgrade, not a hard prerequisite. A first valid confirmed breakout still matters for momentum capture, so the system upgrades to `confirmed_strong` on repeated confirmed leadership instead of suppressing early confirmed signals.
- Keep routine Telegram summaries separate from event-driven alerts. The confirmed-cycle digest exists to prove liveness and show market context; it does not change signal state or cooldown logic.
- Treat the 1-day Telegram review as evidence that the current ranking stack is too reactive for a 3-7 day momentum objective. The next tuning pass should prioritize reducing curvature dominance, replacing raw-price momentum with percentage/log momentum, and reconsidering the dominance gate as a hard blocker.
- Replace the BTC-vs-alt-basket dominance proxy with Binance futures BTCDOM history on `1h`, normalized into `falling / neutral / rising` using a `+-0.2%` neutral band.
- Modulate thresholds by dominance state instead of hard-blocking signals. Dominance is now a score adjustment, not a binary veto.
- Switch momentum to log-return normalization and reduce curvature weighting to `0.15` so the leaderboard is less sensitive to price scale and curvature noise.
- Suppress watchlist Telegram by config if needed, but keep confirmed-cycle summaries and confirmed event alerts enabled so liveness and signal context remain visible.
- Deduplicate confirmed summaries by cycle time so restarts do not resend an already-logged digest.
- Promote the current runtime behavior into a canonical written specification in `SPEC.md` so the repo no longer depends on an implied or historical spec.
- Treat the second Telegram review as evidence that the momentum/log-normalization and weaker-curvature refactor materially improved short-horizon ranking stability. Do not cut curvature further yet; the next likely tuning target is macro information content and confirmed stability behavior, not another immediate ranking rewrite.
- Add `entry_ready` as the trader-facing midpoint entry tier between `emerging` and `confirmed`, and give it explicit knobs so operators can keep it tighter than broad emerging context without turning it into a close-only filter.
- Keep repo hygiene strict: ignore local runtime artifacts, prefer clean git-based VPS deployment over `scp` of a dirty working tree, and remove dead compatibility shims instead of letting them rot.
- Add the first execution layer as a flat `execution.py` scaffold instead of pretending the detector is still purely observational. The signal engine already emits trader-facing tiers; the missing piece was persistence and policy, not another architecture rewrite.
- Enter only on `entry_ready`, not on generic `emerging`, so the first trade action stays tied to the tighter intrabar gate rather than the broad early-warning context.
- Use brutally simple position logic by default: take profit at `+2%` and stop loss at `-2%`, evaluated against the latest available price on every cycle. That is materially simpler and more testable than mixing signal-state exits into the first trading version.
- Record orders and positions in SQLite even though fills are currently simulated. If execution state is not queryable after the fact, the scaffold is operationally useless.
- Do not stop at fake fills once the user explicitly wants real demo trading. The runtime now supports signed private Bybit demo requests, market entry submission, and venue-owned TP/SL management when `EXECUTION_SUBMIT_ORDERS=true`.
- Hand TP/SL ownership to Bybit for real demo orders. Once the venue can manage the exit natively, local price-loop exits become redundant and risk fighting the exchange.
- Send Telegram updates for actual position lifecycle events, at minimum entry and TP/SL exits. Signal-only alerts are not enough once the bot starts pretending to trade.
- Keep local SQLite state even with real demo orders. The bot still needs an internal view of what it believes is open, and that state must be reconciled against Bybit closed-PnL records after venue-managed exits.
- Trust the repo-local `.env` over inherited process env for this project. Silent precedence of stale shell variables made credential debugging needlessly confusing and hid the real source of truth.
- In live demo mode, enforce "one position per ticker" against both local SQLite state and Bybit’s venue state. Local-only duplication protection is not enough if the DB ever falls behind reality.
- Drop the global `MAX_OPEN_POSITIONS` gate from the actual entry logic. It was solving the wrong problem. The meaningful safety rule here is one open position per ticker, not an arbitrary portfolio-wide entry count.
- Size live demo entries from risk, not fixed notional. With a `2%` stop and `1%` risk budget, notional is `available_balance * 0.01 / 0.02`, i.e. about `50%` of available balance per trade.

## 2026-04-06

- Treat a missing repo-root `.env` in a local checkout as a hard configuration failure for execution debugging. If the file is absent, the process may still look half-configured from inherited shell variables while keeping critical execution toggles at their default `false` values.
- When creating a local `.env` from the VPS-oriented template, override `SQLITE_PATH` to a workspace-local path instead of leaving the `/opt/...` example in place. Using the deployment example verbatim on a laptop is sloppy and makes state inspection misleading.
- Do not assume the local SQLite file explains Telegram behavior unless the logged signal kinds and timestamps actually match the chat export. In this session they did not, so the right conclusion was runtime drift, not strategy failure.

## 2026-04-09

- Keep the ranking and signal-generation logic unchanged for now and invert only the execution layer. This makes the current system an explicit contrarian test instead of pretending it has become a different alpha model.
- Use asymmetric short exits of `TP=3%` and `SL=2%` for this inversion pass. If the short bias still fails under those terms, the signal is probably weak rather than merely directionally inverted.
- Put research data in SQLite, not just the app log. Journald is for runtime/debugging; SQLite is the source of truth for daily stats, trade analytics, position marks, and portfolio snapshots.
- Track post-exit follow-through directly in the runtime so TP/SL tuning is based on what trades did after closure, not just whether they closed green or red.
- Use a UTC-day stop-loss breaker (`MAX_DAILY_STOP_LOSSES`) instead of ad hoc manual shutdown when the bot is clearly trading poorly intraday.
- Use CSV export from `report.py` as the VPS retrieval path. Pull flat files with `rsync`; do not make future analysis depend on SSHing in and hand-querying SQLite every time.
- Keep the export-sync automation pull-based from the Mac side. The VPS should remain the source of truth, and the local machine should schedule the backup fetch and analysis pass instead of relying on the server to push files outward.
- Add a separate `MAX_ENTRIES_PER_REBALANCE` gate instead of reviving the dead portfolio-wide open-position cap. The useful control here is "how many fresh names may one emerging batch open," not "pretend portfolio state alone solves correlation."
- Remove dead config surface aggressively when it no longer changes runtime behavior. In this pass that meant dropping `MAX_OPEN_POSITIONS` and `ANALYTICS_EXPORT_DIR` instead of keeping misleading env knobs around for compatibility theater.
- Restore `MAX_OPEN_POSITIONS` once the user explicitly wants it back as a hard safety net. It is still a blunt tool, but that is fine if it is treated honestly as a kill-switch style portfolio cap rather than alpha logic.
- Organize the production env template by impact, not by historical accident. Telegram, execution/risk, and high-impact signal knobs should be visually obvious because those are the settings people actually reach for during live tuning.
- Split Telegram control into signal alerts vs execution events. People often want fills and exits without candidate spam; that requires a dedicated signal-alert master switch, not more overloaded summary toggles.

## 2026-04-10

- Align execution direction with the momentum ranking instead of keeping the short inversion. If the signal is being tested as momentum again, the runtime should buy strength rather than forcing a contrarian expression.
- Use symmetric `TP=2%` and `SL=2%` for the restored long path. It is simpler, consistent with the original momentum framing, and easier to reason about than carrying forward the short experiment's asymmetric `3% / 2%` exits.
- Add the real no-trade logic at the intraday layer, not by making BTC macro thresholds more clever. The practical failure mode here is session type mismatch, so `entry_ready` now requires enough breadth, drift, trend efficiency, and leader persistence before the bot is allowed to treat the day as momentum-tradeable.
- Build the first actual backtest around the live engines, not around a separate spreadsheet model. Even a close-proxy intrabar approximation is more honest than inventing another signal calculator just for historical testing.
- Treat a zero-trade historical run as information, not as success theater. If the current `entry_ready` logic does not fire under a close-proxy replay, then the harness has exposed a real limitation of the approximation and likely a need for minute-level intrabar reconstruction.
- The serious backtest should be minute-aware by default. `entry_ready` is an intrabar concept, so a close-proxy-only harness is not good enough to judge the regime filter or TP/SL quality honestly.
- Use historical minute `high` / `low` for TP/SL evaluation, but process exits before new entries inside each minute. Letting a just-opened trade benefit or suffer from the same candle extremes that happened before the entry close is backtest garbage.
- Keep the simulator portfolio-aware and deliberately conservative:
  - equity-based sizing instead of fixed toy notional
  - modeled fees and slippage
  - gross exposure cap
  - same-minute TP+SL conflicts resolved pessimistically
- Parallelize minute-history fetches. Serializing dozens of symbol-range pulls was wasted runtime, not useful realism.
- Judge the intraday regime filter on longer windows, not tiny samples. On a `32`-bar window the results were identical on and off; on `96` bars the filter cut signals materially and improved net PnL. That is still not proof of robustness, but it is the first nontrivial evidence that the gate may actually help.
- Build sweep mode into the backtest itself instead of doing ad hoc scripts. Repeated historical windows are how this strategy should be judged, because one recent day can flatter or smear the filter too easily.
- Treat missing older candles as a universe-availability problem, not a code failure. If a current ticker did not exist in `2024`, the correct behavior is to skip that full-universe window and say so explicitly.
- Based on the sampled 1-year and 2-year sweeps, the intraday gate should be treated as a selectivity / drawdown-control layer, not as a guaranteed alpha enhancer. It improved average return and drawdown on the sampled windows, but it still lost on some strong momentum sessions by being too restrictive.
- Keep universe cleanup explicit and honest. If a user wants to retain a symbol like `RENDERUSDT` despite missing older history, accept that choice and treat the consequence correctly: older full-universe sweeps must either skip that window or move the start date forward.

## 2026-04-11

- Put historical candle caching inside the existing exchange client instead of creating a separate backtest-only data loader. The backtest should reuse the same fetch surface and simply become cheaper, not grow a second inconsistent source of truth.
- Use SQLite for the backtest candle cache. One year of `1m` candles across the full universe is too large and too repetitive for JSON/CSV junk files, and SQLite gives deduped keyed storage plus cheap reuse across runs.
- Make cache warming a first-class `backtest.py` command (`--prefetch-lookback-days`) instead of a one-off script. If the workflow is not discoverable from the main research entrypoint, it will rot.
- Treat a fully warmed year of `1m` universe data as a storage tradeoff, not a free win. It saves network time on repeated research, but it is large on disk and does not fix the CPU cost of the minute-by-minute replay loop.
- Optimize the actual replay hotspot before shaving smaller edges. The profile made this obvious: Hurst detrending was eating the run, so replacing repeated `np.polyfit` work with an equivalent vectorized linear-detrend path was the highest-value safe optimization.
- Add a separate `research-fast` mode instead of quietly degrading the default backtest. Full mode remains the audited path; fast mode is an explicit choice for broad parameter sweeps where signal-row persistence and SQL summaries are just bookkeeping drag.
- In fast mode, only skip work that does not affect trade or equity correctness:
  - no per-pass signal-row persistence
  - no post-run signal-summary SQL scan
  - no intraday regime scan when the gate is not participating in decisions
  The execution and equity path stays intact.
- Make plan reuse a first-class backtest workflow. Parameter sweeps should fetch/build the historical replay input once, then run multiple settings over that same immutable `MinuteReplayPlan`. Rebuilding the same plan for every variant is just wasted setup time.
- Use separate worker processes, not threads, for variant parallelism. The replay is CPU-heavy, each variant already has naturally isolated state/DB outputs, and thread-level shared-state complexity is not worth the risk for a real-money research path.
- Keep process parallelism portable by snapshotting the prebuilt replay plan to disk once and loading it in workers. On Linux, a `fork`-based model could be even faster, but the file-snapshot approach is safer across local macOS development and avoids hidden event-loop/resource inheritance bugs.
- Keep the serial path async-safe. The first benchmark exposed a real bug where the serial variant path called `asyncio.run()` from inside an active event loop; that is now fixed. This is exactly why “thorough testing” matters more than clever design claims.
- Treat `--variant-workers` as a genuine throughput lever now. On the local 10-core M4, the benchmarked gains were large enough to justify recommending more CPU for research once the sweep grid is big enough.
- Put post-exit TP/SL research inside the simulator, not in a separate analysis script. If the replay engine does not own the after-close follow-through data, the exported trade ledger will drift from the actual modeled execution path.
- Track post-exit behavior with a bounded number of future intrabar bars (`ANALYTICS_POST_EXIT_BARS`) and summarize it conservatively as best move, worst move, and realized close-to-close volatility. That is enough to study TP/SL quality without pretending minute candles are a full tick-level truth source.
- Store the research discipline in the repo, not in conversation memory. A real-money system needs an explicit optimization plan so later sessions do not improvise giant grids, optimize exits before entries, or report in-sample winners as if they were robust.
- Use one-family sweeps as the first pruning pass, then move to targeted pairwise interaction checks for survivor knobs. Full-factor brute force across everything is computationally expensive and intellectually sloppy.
- Simplify the live strategy contract instead of half-maintaining two philosophies. `entry_ready` is now the only tradeable signal tier; confirmed bars still advance history, but they no longer pretend to add a second actionable lifecycle.
- Keep residual momentum in the live engine, not just in research notes. The default ranking mode is now `basket_relative` because raw absolute momentum was too willing to collapse into generic alt beta.
- Add a blunt cluster cap (`MAX_POSITIONS_PER_CLUSTER`) before inventing more exotic correlation controls. A manual cluster map is imperfect, but it is still more honest than pretending raw position count alone controls concentration.
- Make long-horizon backtests honest about listing history. `BACKTEST_UNIVERSE_POLICY=window_available` is the default because pretending every current symbol existed across old windows is worse than running on a smaller active universe for that slice.
- Put walk-forward validation and live-vs-backtest reconciliation in the repo itself. If those workflows live only in chat, they will not be used consistently when real money is at risk.
- Keep the live trading contract brutally simple for now. `entry_ready` is the only tradeable tier, and confirmed-bar logic stays as state advancement and analytics instead of pretending to be a second entry philosophy.
- Move the default residual ranking from `basket_relative` to `cluster_relative`. Comparing a symbol to recent correlation peers is a better default than comparing it to the whole basket when the real problem is generic beta pile-up.
- Let cluster assignment be configurable (`manual`, `dynamic`, `hybrid`) and feed both the ranking reference and the exposure cap from the same current grouping. Static clusters alone are too stale; dynamic clusters alone need a safe fallback.
- Put richer entry diagnostics directly into live trade notes and backtest trade exports. If a trade cannot explain why it passed, later TP/SL or filter tuning turns into superstition.
- Add built-in stress profiles to the backtester instead of relying on ad hoc “mentally imagine worse costs” arguments. Real-money research needs a repeatable hostile-case path.
- Export the full walk-forward candidate leaderboard, not just the single winner. If the runner-up is nearly as good but much more stable, the repo should preserve that information.
- Do the first production-hardening pass around explicit controls and observability, not around a big repo rewrite. That means startup validation, manifests, health snapshots, runtime events, and drift checks before deeper architecture work.
- Keep the control-plane modules flat (`runtime_validation.py`, `runtime_monitor.py`, `monitor.py`) instead of inventing package layers. The repo benefits from clearer boundaries, not from enterprise cosplay.
- Add `OPERATOR_PAUSE_NEW_ENTRIES` as the simplest honest manual brake. If someone wants the bot to keep observing but stop taking fresh risk, that should be a first-class control, not a hacky env dance.
- Stop telling local research machines to copy the production template verbatim. The VPS template is the canonical knob list, but a PC needs localized paths and safe defaults. That should be generated mechanically, not remembered manually.
- Treat the warmed candle cache as transferable infrastructure. Packing and restoring a 1-year SQLite cache is saner than redownloading 30M rows on every machine, and much saner than trying to commit the blob to git.
- Keep the Windows research-box bootstrap blunt and small: one PowerShell script is enough. This repo does not need a second installer framework just to create a venv, localize `.env`, optionally restore cache, and run tests.
- Treat `pyflakes` cleanliness as part of repo hygiene. If the codebase carries undefined local annotations or unused imports, that is not harmless noise; it is a signal that we are losing track of what is actually live.
- Remove duplicated configuration knobs when only one of them is real. `CANDLE_INTERVAL_MINUTES` was just shadowing `CANDLE_INTERVAL`, so the repo now derives the interval milliseconds from the canonical string field instead of pretending two env vars need to stay synchronized.
- Keep legacy schema/report compatibility shims only where they still serve historical SQLite data. Do not “deep clean” them away casually just because the live runtime no longer emits those paths.
- For the old confirmed-era signal history, the right compromise is normalization, not theater. Reports now bucket `confirmed` / `confirmed_strong` under one `legacy_confirmed` label so old DBs remain readable without implying those are still live signal tiers.
- Stop writing dead confirmation fields in the live path once the strategy contract no longer uses them. Historical DB columns may still exist, but the runtime should not keep propagating meaningless `confirmation_signal_kind` / `confirmed_at` values forever.
- Do not let ordinary full-run/grid backtests silently anchor to wall-clock "now" once a large cache has been prefetched. That creates pointless tail fetches, more Bybit rate-limit warnings, and non-deterministic reuse of the warmed dataset.
- Reuse the existing window-pinned intrabar fetch path for ordinary single-run, comparison, and grid backtests instead of maintaining two different anchoring behaviors. The logic already existed for sweeps and walk-forward runs; the bug was that the default CLI path ignored it.
- Long backtest runs need blunt phase messages in stdout. Operators should not have to infer "is it fetching, building a plan, running variants, or exporting?" from Wi-Fi graphs or CPU usage.
- Long grid runs also need resumable state, not just prettier output. `variant_summary.csv` is now the checkpoint surface; if the run dies halfway through, resume from completed variants instead of paying the full replay cost again.
- Cache visibility should come from the market-data client, not from guesswork in the backtester. The client now owns hit/miss and network-request counters so the operator sees real cache behavior.
- Reconciliation should be part of the normal backtest workflow, not a separate ritual people forget to run. `backtest.py` now has a first-class Telegram reconciliation hook for exported runs.
- Aggregate reconciliation metrics are not enough. Alignment breaks ticker by ticker, so the reconciliation layer now treats per-ticker summaries as a first-class export instead of making the operator reverse-engineer mismatches from raw unmatched rows.
- Daily forward-vs-backtest checks should use the live SQLite trade log, not Telegram HTML, whenever possible. Telegram exports are useful for ad hoc forensic work; the operational end-of-day path should consume `trade_analytics` directly and persist the result back into SQLite.
- Keep daily reconciliation out of `main.py`. It belongs in a separate cron/systemd path so alignment checks do not interfere with the live trading loop.
- Treat VPS reconciliation artifacts as part of the normal pull-sync bundle, not a second manual retrieval workflow. If daily forward-vs-backtest exports live only on the server, they will be forgotten; `deploy/sync_exports.sh` should pull them to the local analysis machine by default.
- Long grids need throughput visibility at the variant level, not just phase banners. The runner now logs per-variant completion timing, average variant runtime, and ETA so operators can distinguish "slow but progressing" from "stalled".
- Long grids also need a memory sanity check before spawning workers. On smaller Windows boxes, the snapshot/unpickle cost of an annual replay plan can exceed RAM long before CPU becomes the real bottleneck, so the runner should reduce worker count instead of dying late with `MemoryError`.

## 2026-04-12

- Stop paying SQLite cost anywhere in the comprehensive replay signal path when the database is only serving signal summaries. The right compromise is an in-memory summary sink during replay, not per-cycle persistence and not an end-of-run SQLite flush.
- Keep the full-mode report contract intact without round-tripping through SQLite. The comprehensive path now builds `ReportSummary` directly from the in-memory aggregate and only uses the database again where it still serves a real purpose.
- Do not raw-pickle the full `MinuteReplayPlan` object graph into every worker. The worker snapshot should use a compact primitive payload, especially for `HistoricalCandle` buckets, and it should not duplicate `btc_daily_history` that is already present inside `confirmed_plan`.
- Treat this as a first-pass memory fix, not a fantasy claim that worker duplication is solved completely. Workers still rebuild their own replay plan in memory; the point of this change is to cut snapshot size and load overhead materially without a bigger data-layout rewrite.
- Keep local research artifacts in a dedicated ignored folder, not in the repo root. Big backtest DBs and export trees are operational data, not source files, and mixing them into the top-level workspace just makes cleanup and inspection worse.
- Single-run backtests need progress output inside the replay loop, not just phase banners before and after it. The useful contract is percent complete, completed bars, elapsed time, and ETA while the actual simulation is running.
- Do not solve the TP-then-immediate-reentry problem with a real trailing stop. The v1-safe version is a one-step profit-protection ratchet: if a long is still strong near TP, extend the target once and lift the stop into profit.
- Keep profit protection state explicit on open positions. Current TP, current SL, and ratchet count must survive across cycles and restarts, otherwise live venue sync and exit classification turn into guesswork.
- Keep the simulator conservative relative to live. The live engine can update Bybit stops on the current emerging cycle; the backtest applies the same ratchet from prior intrabar signal state so it does not retroactively rewrite the same candle after seeing the full close.
- Protected exits should be labeled separately from raw stop losses. If a ratcheted stop takes you out above entry, calling it a stop loss is analytically dishonest.
- Do not let profitable exits immediately churn back into the same ticker. A short same-ticker cooldown after profitable exits is a cleaner v1 control than pretending a fresh `entry_ready` signal minutes later is always a new opportunity.
- Same-ticker revenge trading is a separate failure mode from global daily-stop logic. Cap same-name losers per UTC day explicitly instead of waiting for portfolio-wide stop counters to catch a single churny ticker.
- Stale winners should exit only after they have already earned the right to be treated as winners. The stale-winner path therefore requires profit protection first by default; otherwise it would just become another discretionary early-exit rule.
- Exit analytics need exit-state fields, not just more raw signal spam. Recording exit-time rank/composite/momentum/curvature/Hurst plus active TP/SL and ratchet count is the useful data-driven compromise.
- Venue close reconciliation must preserve intentional exit reasons. If the engine submitted a reduce-only stale-winner exit, the later Bybit sync path should not relabel it as a generic venue close just because the final close was observed asynchronously.

## 2026-04-13

- A profitable same-ticker re-entry should not be allowed merely because the cooldown expired. Treat it as a fresh setup only if it is measurably better than the last profitable exit, using exit-state rank and/or composite improvement rather than vague “still strong” logic.
- The re-entry gate should be permissive only when there is no useful prior exit-state information. If the last profitable exit has no exit rank or exit composite recorded, allow the trade rather than inventing a fake block.
- Time-to-first-profit is worth tracking because it supports a real stale-trade hypothesis without adding a discretionary exit rule first. The repo now records those milestones before trying to optimize around them.
- Ticker-level fee-farm analysis belongs in the backtest outputs, not in hand-run spreadsheets after the fact. Per-ticker fees, slippage, expectancy, and win rate are now first-class backtest summary fields.
- Do not add a made-up “regime slippage model” just because Reddit likes harsher backtests. Stress modeling is only worth adding when the scaling rule is defendable from repo data; arbitrary regime penalties would be cargo culting.
- Stability screening should sit on top of the existing grid runner, not become a separate optimization engine. The useful contract is robust neighborhood summaries and per-setting robustness exports, not another bespoke search mode to maintain.
- Prefer median / profitable-fraction / worst-drawdown style robustness metrics over “best row wins.” That is the cleanest way to penalize fragile parameter pockets without pretending the repo can certify true robustness from one number.
- Treat volatility expansion as a regime-filter experiment, not as an excuse for dynamic stops or ATR sizing. The honest v1-safe implementation is an optional blocker on `entry_ready`, not another adaptive risk engine.
- Do not fake true ATR when the regime path only has close series. The repo now uses a close-only ATR-style ratio on the normalized basket path and names it accordingly in docs instead of pretending the data is richer than it is.
- The immediate research priority is no longer “add features.” It is: prove net edge after costs, validate entry quality, measure anti-churn opportunity cost, and prune weak filters.
- Hurst, VEI, and every intraday regime sub-check are on probation. If isolated testing does not justify them, cut them instead of preserving them for sophistication theater.
- Keep the structural core unless data disproves it: residual/cluster-relative momentum, cluster exposure caps, fixed TP/SL, and one-step profit protection are currently more defensible than extra gatekeeping layers.
- Promote the winning 30-day grid row into the active baseline instead of continuing to tune around weaker `pass_count=3` variants:
  - `ENTRY_READY_MIN_COMPOSITE_GAIN=0.00`
  - `ENTRY_READY_MIN_OBSERVATIONS=3`
  - `INTRADAY_REGIME_MIN_PASS_COUNT=4`
- Treat VEI hard-gating as the honest test of the idea. Soft-vote VEI remains available for comparison, but it is too weak to answer whether volatility expansion should truly block `entry_ready`.
- Keep VEI off by default. If hard-gate tests do not improve net results or robustness materially, deprioritize it instead of stacking more volatility heuristics.
