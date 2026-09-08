# Annual strategy and execution review

## Purpose

Reconstruct the completed UTC year ending September 8, 2026, and separate historical strategy economics from the LONG demo execution experiment.

## Spec Tables

| Decision | Scope / evidence |
| --- | --- |
| LONG demo execution | Join the near touch with GTC for 30 s, including one-tick spreads; at most one passive amend after 15 s; cross the remainder through the existing bounded planner, with 20 s cross grace |
| Selection | `441811eb227b1084eaa3343d3d10428994425481`, committed 2026-09-08 09:53:38 UTC; actual activation and host observations are in [STATE](../../STATE.md) |
| Recorded-book comparison | Selection uses five usable LONG openings; all eight queue/latency cells save 6.063–7.309 bp versus simulated crossing; all complete, maker share 87.4–100%. These orders shaped the choice and cannot grade it |
| Installed study | `441811eb` at 10:25:30 UTC: 24 aligned orders / 56 identified fills, thirteen complete comparisons, six usable LONG openings; all eight LONG candidate cells save 6.435–7.407 bp with 89.0–100% modeled maker share. These remain pre-commit order data, not forward grading |
| Alternative | PostOnly with 30 s patience remains a candidate; GTC uses the existing native amend/cross lifecycle and can take on arrival. A perpetual two-sided market maker has no new evidence from this annual run |
| Adverse selection | Fill-conditioned future markouts are evaluation labels. A future toxicity filter must use only book/flow information available before placement; this change does not introduce that filter |
| Funded execution | Mainnet LONG still crosses; CARRY and EXODUS retain their policy selection. The worked-entry restart cancellation repair applies to both realms |

| Backtest | Window / sizing | Net return | Daily max drawdown | Daily Sharpe | Interpretation |
| --- | --- | ---: | ---: | ---: | --- |
| LONG v12, minute execution | 2025-09-08 through 2026-09-07; 365 days; $100 starting equity; current 6× sizing dial | +83.8648% | −7.5760% | 1.6950 | 43 trades, 37 resizes; minute trade/mark-price execution bound; crossing cost assumption |
| LONG v12, standard hourly research | Same requested year; research notional normalization | +12.2606% | −1.3575% | 1.4435 | Coarse companion only; different execution/sizing mechanics, partial terminal funding; not an execution-policy comparison |
| CARRY v7, hourly research book | Same 365 days; 1× notional-normalized return | −9.1114% | −30.5724% | −0.0563 | Native reducer; 1,194 planned resizes, 4,975 replay-wide unpriced target/exit attempts skipped; no historical pre-settlement observations |

| CARRY reconciliation | Verified result / boundary |
| --- | --- |
| July 28 funding bug | Commit `3540f9a5c7d6c6e7df8ca05b1854a46fbd17dbe5` replaces `age < 1.0`, which counts float-epsilon ages near one hour as another settlement, with the reset detector. The affected earlier carry benchmark Sharpe falls from 2.56 to 1.21. Current epsilon, one-hour and missing-bar regressions pass |
| Later profitable evidence | The September 2 stored v7 summary reports +2,192.70% over 2,052 daily observations and raw Sharpe 1.5907 using the already-corrected funding scorer. The July defect cannot by itself explain disagreement with that later result |
| Refreshed daily control | Same fresh panel and v7 daily rule, 2025-09-08 through 2026-09-06: 364 score days; +297.1576% at the registered 7.78 bp per side; +275.9041%, drawdown −17.3242%, Sharpe 2.1032 at 14.357519 bp per side. The last requested day lacks its next full daily mark and is explicitly absent |
| Single-change hourly control | Disabling only `early_exit_enabled` inside the same research harness changes full-year net −9.1114% to +148.6933%, drawdown −26.7386%, Sharpe 1.8407; zero-cost return +187.4703%. No live config is changed; the 4,975 unpriced attempts and absent pre-settlement observations remain |
| Matched 364-day comparison | Daily holdings +275.9041%; hourly without intraday funding exits +149.4177%; current hourly −8.8467%, all at 14.357519 bp per side. The single missing terminal day does not explain the gap |
| Decision | Reconcile the live pre-settlement exit and EXODUS handoff economics before changing CARRY exits. Fees explain only a small part of the matched-model gap; the current hourly settled-exit approximation is not a full replay of that live sequence |
| Daily gross and costs | Zero-cost compounded return +323.8466%; 83.9047× one-way turnover. Summed daily weighted price/funding contributions are −1,782.079 / +18,663.328 bp before trading costs; these are additive contributions, not compounded returns |
| Daily eras at 14.357519 bp | September–December 2025 +37.7657%; January–September 6, 2026 +172.8576% |
| Model comparison | Daily target holdings and the native hourly lifecycle are different positions through time. The daily scorer also filters unavailable forward-return rows before ranking; it remains a research comparator, not proof of live performance. The panel's bar labels precede close availability by one hour; hourly price/settlement clocks remain an approximation |

| EXODUS evidence | Verified result / boundary |
| --- | --- |
| Current trigger | Consume the durable CARRY pre-settlement exit event, short the attributed quantity at the fire, cover at settlement +60 minutes. Settlement +5 minutes is the entry cutoff, not the normal entry anchor |
| Registered estimate | Config retains 1,112 selected events and +95.2 bp mean on clean events after 15.56 bp round-trip fees; slippage, impact and partial fills are omitted. Its historical fire-population scratch programs are not retained in the repository |
| Independent retained reconstruction | 560 proxy fires, 2023 through July 2026; 9,871 of 14,050 held settlements have intervals under four hours and are excluded. The sample does not reconstruct the live trigger population |
| Trigger comparison | On 168 comparable ticker/settlement pairs, the proxy agrees with the displayed trigger 92.26% of the time; 7 of 49 proxy fires are false. Only 1 of 48 displayed fires settles deeply negative. A noise-driven proxy cannot grade the premature-fire veto |
| Proxy cost sensitivity | Repricing the retained 2026 subset at recent EXODUS costs changes +5.958 bp/event to −11.408 bp/event; this diagnoses fee sensitivity of that proxy, not actual EXODUS alpha. All 2023–2026 and cost cells are retained in `exodus-review/historical_proxy_cost_grid.csv` |
| Actual recent fills | Reconciled 48 h window: 28 identified CAPUSDT fills have equal buy/sell quantity 7,440; gross price P&L +0.193600 USDT, paid fees −0.72467318, net −0.53107318 before funding. Actual fill prices already include slippage; do not deduct it twice |
| Annual limitation | A valid annual native EXODUS backtest needs fire-time running-rate observations and CARRY-attributed quantity. Klines, trade archives and settled funding cannot supply that historical observation sequence. No annual EXODUS return is fabricated |

| LONG minute cash accounting | USDT |
| --- | ---: |
| Starting equity | 100.000000 |
| Gross price P&L | +89.430459 |
| Trading fees | −4.169432 |
| Slippage | −1.618125 |
| Funding | +0.221934 |
| Net P&L / final equity | +83.864836 / 183.864836 |
| Executed mutation notional | 4,031.028579 |

| CARRY cost sensitivity, same quantity path | Net return | Daily drawdown |
| --- | ---: | ---: |
| 0 bp per side; price plus funding only | +5.7834% | −28.5570% |
| 3.6 bp per side; fee-only idealization | +1.8347% | −29.0676% |
| 8.227502 bp per side; recent five-fill CARRY sample | −3.0261% | −29.7188% |
| 14.357519 bp per side; uniform annual scenario | −9.1114% | −30.5724% |
| 20 bp per side; cost stress | −14.3761% | −31.3491% |

| CARRY era | Net return |
| --- | ---: |
| September–December 2025 | +12.6185% |
| January–September 7, 2026 | −19.2951% |
| First / second half | +11.5387% / −18.5138% |

| CARRY accounting / decision | Value |
| --- | --- |
| One-way turnover | 105.7254× the notional base over the scored year |
| Uniform-cost P&L per 100 normalized starting units | +7.285581 price/funding contribution −16.397000 execution-cost contribution = −9.111419 net; contributions use the same evolving net-equity basis |
| Break-even cost | 5.320282 bp per side, with this fixed reconstructed quantity path; this is not an achievable-fill claim |
| CARRY interpretation | The daily research control remains profitable on these refreshed inputs. The hourly reconstruction changes the holding path and lacks pre-settlement observations; its −9.11% is not evidence that fees or the July funding correction erased the registered strategy edge |

| LONG concentration | Result |
| --- | --- |
| September–December 2025 | −2.7077% |
| January–September 7, 2026 | +88.9818% |
| First / second half | +12.3562% / +63.6447% |
| August 2026 | +62.99%; August entries contribute +73.9560 USDT of the full-period +83.8648 USDT net P&L |
| Cutoff | Three remaining trades liquidate at the final available minute, including modeled costs; this is a reporting assumption, not venue realization |

| Cost calibration | Scope / value |
| --- | --- |
| Annual uniform scenario | 10.34334519 bp fee + 4.01417357 bp slippage = 14.35751876 bp per executed side; funding separate |
| Historical authority | These are observed recent costs applied as a constant annual scenario, not an annual account fee ledger |
| Reconciled actual sample | 48 h ending 2026-09-08 07:47:54 UTC; 54 identified fills / 23 orders match Bybit executions and transactions; 100% cost coverage |
| Actual fee / slippage / total | 1.14046812 / 0.442607 / 1.58307512 USDT on 1,102.610518 USDT arrival notional; 14.357519 bp per executed side |
| Actual sleeve costs | LONG 12.252049 bp; CARRY 8.227502 bp; EXODUS 16.462955 bp. CARRY has only five fills in this sample; the aggregate annual scenario deliberately differs from this sleeve sample |
| Actual funding | +0.34536423 USDT over 55 funding settlements in the same account window, outside execution costs |

| Inputs / reconstruction | Coverage and boundary |
| --- | --- |
| Durable artifact root | `~/SHARED_DATA/bybit_full_pit/reports/annual_20260908/` |
| LONG rule / data identities | `long-minute/long_live_physics_report.json` and its 32-file source snapshot; effective config SHA256 `bfba69da241bef04f670d2cdeb405efc3eb8d7669bfafd77a7079bc17b37a105`; source commit `441811eb`; unrelated dirty `CLAUDE.md` remains outside the committed change |
| Bybit hourly refresh | 6,757 missing symbol-days requested; 6,746 downloaded, including 20 trade-archive fallbacks; eleven return no API bars and HTTP 404 from the public trade archives |
| Missing PIT days | BLUAIUSDT 2025-10-23; MONUSDT 2025-11-24; DATAUSDT 2026-06-30 through 2026-07-08. Required feature-window pairs: 259,542; covered: 259,531. Membership remains applied before features |
| LONG execution data | 298 symbol-days / 429,120 minute rows per trade and mark stream; zero required minute gaps; 45/45 funding download intervals covered; all 43 trades have modeled funding |
| Bybit ancillary source error | Premium-index response for 1000000MOGUSDT at `1788116400000` has invalid OHLC and is rejected; no repair or invented price enters the input |
| Binance refresh | Daily-price archive topup adds 201,382 rows; unicode URL repair retrieves 62 metrics symbol-days, with 48 archive absences and zero remaining request failures. The five repaired symbols are outside this CARRY panel population |
| CARRY population / timing | Intersection of the historical both-venue research inputs; different from the full live Bybit universe. Hourly marks and settled funding approximate execution; historical pre-settlement running rates are absent. Binance metrics assume next-midnight availability; original receiver times are not reconstructed |
| CARRY normalization | Current 3× sizing enters native targets, but scores divide by `equity_usdt * notional_multiplier`; default replay equity is 1,000,000 USDT. Its standard curve is a notional-normalized research book, not a $100 funded-account simulation |
| Evidence note | Lane 1; existing rules and current selection use seen history, including this year. No unseen annual grading, maker-fill proof, EXODUS reconstruction or joint funded-account P&L is claimed |

| Artifact | Relative path under the artifact root |
| --- | --- |
| LONG minute curve / ledger | `long-minute/long_minute_equity_btc.png`, `long_live_physics_daily_equity.csv`, `long_live_physics_trades.csv`, `long_live_physics_mutations.csv`, `long_live_physics_funding.csv` |
| LONG accounting / monthly returns | `long-minute/independent_accounting_check.json`, `long-minute/monthly_returns.csv` |
| Standard LONG companion | `standard/long/long_native_research_report.json`, `standard/long/long_native_equity_btc.png` |
| CARRY final reconstruction | `standard-extended/carry/`: `lane2_carry_hold_v7_daily_scores.csv`, `lane2_carry_hold_v7_daily_equity.csv`, `lane2_carry_hold_v7_summary.json`, `cost_sensitivity.json`, `monthly_returns.csv`, `era_returns.csv` |
| Actual and hypothetical execution | `actual-costs/reconciled-costs.json`, `actual-costs/account.json`; `execution-selection/latest.json` retains the selection sample; `execution-deployed/` records the installed study |
| CARRY reconciliation / EXODUS review | `carry-reconciliation/comparison.json`, `matched_model_cost_grid.csv`, `carry_model_comparison.png`, `registered_daily_scores.csv`, `registered_attribution.csv`; `exodus-review/summary.json`, `historical_proxy_cost_grid.csv` |
| CARRY panel | `panel-extended/index.json`; individual shard manifests retain hashes and construction windows |
| Downloads | Canonical parent `reports/archive_klines_1h_api_annual-20260908.*`, `reports/archive_manifest_annual-20260908.*`; compact LONG inputs retain `_source.json` identities |

## Invariants

- Must keep gross, fees, slippage and funding on the same accounting basis; independent trade sums and daily equity reproduce the LONG cash result to less than `1e-9` USDT.
- Must report every tested execution latency/queue cell and every historical era, including losses and missing data.
- Must keep the annual crossing-cost reconstruction separate from the new passive execution experiment; the +83.8648% return includes no hypothetical maker savings.
- Must not replace missing historical prices or claim coverage because delisted names appear in trades.
- Must distinguish the CARRY notional-normalized research curve from LONG's $100 account reconstruction; no combined-account curve is produced.

## Operational Recipes

```bash
export ANNUAL_ROOT="$HOME/SHARED_DATA/bybit_full_pit/reports/annual_20260908"
export LIQUIDITY_MIGRATION_STRATEGY_CONTRACT_BIN="$PWD/engine/target/debug/strategy_contract"
export POLARS_MAX_THREADS=3
rustup run 1.90.0 cargo build --manifest-path engine/Cargo.toml \
  -p engine-strategies --bin strategy_contract

.venv/bin/python scripts/research/run_long_live_physics.py \
  --data-root "$ANNUAL_ROOT/inputs/bybit" \
  --start 2025-09-08 --end 2026-09-08 --execution-end 2026-09-08 \
  --initial-equity-usdt 100 --taker-fee-bps 10.34334519 --slippage-bps 4.01417357 \
  --report-dir "$ANNUAL_ROOT/long-minute"
PYTHONPATH="$PWD" .venv/bin/python "$ANNUAL_ROOT/long-minute/render_curve.py"

bash scripts/research/equity_curves.sh --sleeves carry \
  --start 2025-09-08 --end 2026-09-08 --panel-root "$ANNUAL_ROOT/panel-extended" \
  --all-in-cost-bps 14.35751876 --out "$ANNUAL_ROOT/standard-extended"

PYTHONPATH="$PWD" .venv/bin/python "$ANNUAL_ROOT/standard-extended/carry/cost_sensitivity.py"
PYTHONPATH="$PWD" .venv/bin/python "$ANNUAL_ROOT/standard-extended/carry/render_curve.py"

# Reconciliation controls and the retained EXODUS proxy review:
.venv/bin/python "$ANNUAL_ROOT/carry-reconciliation/reproduce_daily.py"
.venv/bin/python "$ANNUAL_ROOT/carry-reconciliation/reproduce_clock_controls.py"
.venv/bin/python "$ANNUAL_ROOT/carry-reconciliation/summarize_comparison.py"
.venv/bin/python "$ANNUAL_ROOT/carry-reconciliation/render_comparison.py"
.venv/bin/python "$ANNUAL_ROOT/exodus-review/reproduce_review.py"

scripts/ops.sh execution-study --json
```
