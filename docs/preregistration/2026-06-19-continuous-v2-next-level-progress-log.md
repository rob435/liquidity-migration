# Continuous V2 Next-Level — Program Execution Log

Living tracker for the operator-directed next-level Continuous V2 research push.
Parent plan: `docs/preregistration/2026-06-19-continuous-v2-next-level-ab-research-plan.md`.
This log is the resumable backbone: a fresh session reads STATE.md + this file +
the cited receipts to know exactly where the program stands and what is next.

**Started:** 2026-06-19. **Owner:** rob435. **Real money:** false (unchanged).
**Honest framing:** mining is allowed; fooling ourselves is not. Every apparent
edge needs a falsifier; every venue split needs an explanation; every win must
survive costs, path ambiguity, delayed features, and hash/null controls. The
prior deep A/B pass already closed v2 mechanism mining on 1h bars with **no
candidate** — the next-level premise is that 1m path-fidelity + execution
realism could change which mechanisms are even measurable, not that alpha is
expected. Forward demo/paper stays the only OOS arbiter.

## Wave status

| Wave | Scope | Status |
|------|-------|--------|
| 0 | Freeze + reproduce both controls | **COMPLETE** (both baselines reproduced, hashes verified, bundle written) |
| 1 | 1m PIT data foundation (trade-window-scoped) | **COMPLETE** — 100% coverage, 0 gaps, 0 checksum fail (176 MB) |
| 2 | 1m / trade-aware execution engine + order-fill ledger | **X1 engine DONE + validated** (1m vs 1h: 100% reason agree); X2 driver + Book A next |
| 3 | Feature Almanac V3 (`data_available_ts`) | not started |
| 4 | A/B books on the 1m engine | **Books A (stops) + B (admission) CLOSED — no both-venue candidate** |
| 5 | No-order forward shadow | gated on a Wave-4 candidate |

## Wave 0 — baseline freeze (in progress)

Receipt: `docs/preregistration/2026-06-19-continuous-v2-phase0-baseline-construction.md`.

Done this session:
- Verified the runner already reproduces `V2_LIVE_RESEARCH_CONTROL` (= existing
  `V2_CONTROL`: TP 0.12 + `frozen_rebalance_rule().enabled=False`).
- Wired `V2_EVIDENCE_ANCHOR` (TP 0.10 + vol adjuster ON max4) as a first-class
  Phase-0 arm via a backward-compatible `rebalance_rule` kwarg on
  `build_full_ledger` (default None → frozen, byte-identical for the live
  forward clock). Frozen config hash unchanged: `c4eb2eed…a4f6`.
- Unit tests green (`tests/test_continuous_v2_evidence_anchor.py`, 5 passed),
  ruff clean.
- Config hashes: `V2_CONTROL = bfa8d385210d`, `V2_EVIDENCE_ANCHOR = 6579c8ece3bb`.
- Launched the full-window both-venue freeze run (background, resumable):
  `backtest-runs/continuous_v2_phase0_freeze_2026-06-19`.
- Proof-of-life (first arm-venue complete): `V2_CONTROL/bybit` reproduced with
  `config_hash bfa8d385210d` (matches the offline-computed hash) and
  `profile_hash c4eb2eed…` — 2367 trades, total_return 0.260, max_drawdown
  -0.013, MAR 6.39, sharpe_like 4.44, worst_day -0.93%, 2023-04-01 → 2026-06-12.
  In-sample working-dataset diagnostics (2f-hedged), NOT promotion evidence.
  Remaining arm-venues still building (heavy: ~6 min/component, ~14 GB RAM).

Result (run complete 2026-06-20, exit 0, runs=4) — **all acceptance PASS**:
- `V2_CONTROL` reproduced (hash `bfa8d385210d`): bybit tot 0.260 / dd -1.3% / MAR
  6.39 / 2367 trades; binance 0.185 / -1.4% / 4.16 / 2149.
- `V2_EVIDENCE_ANCHOR` (hash `6579c8ece3bb`): bybit 0.974 / -5.5% / 5.66; binance
  0.840 / -3.3% / 8.19. Same `n_trades` per venue as control (entries unchanged →
  the anchor differs ONLY by {TP10, vol-adjuster max4}).
- Block-bootstrap anchor−control MAR delta: binance **+4.03**, bybit **−0.73**
  (the documented venue split, now frozen for both baselines).
- Bundle written to the run root: `baseline_manifest.json`,
  `baseline_replay_{bybit,binance}.csv`, `baseline_diff.md`.
- Honest read: in-sample 2f-hedged diagnostics (tiny dd / high MAR), NOT
  promotion evidence. The vol-adjuster is a gross lever (~4× return & drawdown),
  not new alpha; MAR is the honest comparison.

**Wave 1 is now the active wave.**

## Wave 1 — data feasibility finding (IMPORTANT, corrects a prior assumption)

The next-level premise required full 1m PIT for BOTH venues. Current built state:

| Root | 1h klines | 5m klines | 1m klines |
|------|-----------|-----------|-----------|
| Bybit | ✅ 2021-01-01 → 2026-06-11 | ✅ 2023-04-01 → 2026-05-01 | ❌ (only a partial `_w6_entry_1m_cache` of event snippets) |
| Binance | ✅ 2020-01-01 → 2026-06-11 | ❌ | ❌ |

So neither 1m root is built yet. **But both source archives are reachable from
this box** (probed 2026-06-19):

- **Bybit public trade archive** `public.bybit.com/trading/<SYM>/` → HTTP 200,
  daily trade CSVs through 2026-06-18. The CLI already has
  `archive-download-klines` ("build 1m klines from Bybit public trade
  archives"). Phase 1 D1 is feasible.
- **Binance Vision** `data.binance.vision/data/futures/um/daily/klines/<SYM>/1m/`
  → HTTP 200, and **checksum-validated end-to-end** (2026-06-19): downloaded
  `BTCUSDT-1m-2026-06-10.zip`, sha256 `8107fa00…22d5` matched the `.CHECKSUM`
  sidecar; unzipped to a clean full-day 1m OHLCV CSV (1440 rows + header,
  columns `open_time,open,high,low,close,volume,close_time,quote_volume,count,
  taker_buy_volume,taker_buy_quote_volume,ignore`). Phase 1 D2 source integrity
  is proven.

Correction to a prior assumption: STATE/research notes say "Binance ingestion
needs a permitted-region host." That constraint applies to **Binance FAPI REST**
(live funding/OI/liquidation API), **not** to `data.binance.vision` (the public
CDN archive the plan's D2 actually specifies). So **both-venue 1m OHLC is
buildable here** — the next-level program is NOT structurally data-gated for 1m
klines. (FAPI-sourced ancillary — funding/OI/liquidations — remains the
region-sensitive surface and is already largely covered by the existing roots;
Wave 1 D3 will ledger any gaps as blocked rather than silently fill.)

Remaining engineering caveat (not a data-availability blocker): `binance_vision.py`
is currently 1h-shaped and must be extended to `--interval 1m` per D2; the Bybit
`archive-download-klines` 1m-from-trades path exists but must be audited for
dense-day coverage and PIT lifecycle gaps. The checksum-validated sample above
removes the source-integrity uncertainty.

### Wave 1 — construction design (registered 2026-06-20)

Receipt: `docs/preregistration/2026-06-20-continuous-v2-1m-data-foundation-construction.md`.

**Scoping decision (deliberate, documented):** build a **trade-window-scoped** 1m
cache, not a full-universe root. Sized from the Phase 0 trade ledgers: bybit 2401
symbol-day partitions (~141 MB), binance 2238 (~131 MB) — exactly the
`[entry_date−1 … exit_date]` partitions the book's trades touch. Sufficient for
Phase 2 intrabar + Books A/C/E; full-universe root deferred as a registered
optional extension. Manifests persisted at
`~/SHARED_DATA/continuous_v2_1m/coverage_needed_{venue}.parquet`.

**Reuse identified (no ingestion from scratch):** Bybit →
`ingestion.aggregate_trade_klines_1m` + `densify_trade_klines_1m` +
`archive.read_public_trade_archive`; Binance → `binance_vision._fetch_expected_sha256`
+ `_verify_download` (sha256 gate) pointed at the daily-1m Vision URL.

**Next (Wave 1 build, next iteration with fresh context for correctness):**
implement `scripts/continuous_v2_build_1m_trade_windows.py`, smoke on a few
symbol-days/venue, run the targeted build (background, resumable, checksum-gated,
gap-ledgered), then produce the D4 coverage ledger. Deferred deliberately — a
buggy 1m build (bad aggregation / missed PIT gap / unverified checksum) would
silently poison every downstream intrabar book, so it gets careful fresh-context
implementation, not a rushed tail-of-turn one.

**Result (build complete 2026-06-20):** 100% coverage, zero gaps, zero checksum
failures. bybit 2401 partitions / 3,457,440 rows (all 1440-row days); binance 2238
/ 3,222,720 (all 1440); 0 missing, 0 checksum-fail; 176 MB. Build script committed
(`8fbd543`), smoke-validated. `missing_partitions.csv` empty. Bybit no-trade
minutes carry-forward-densified (med 10/day, ledgered); Binance fully dense (med 0).
Audit at `~/SHARED_DATA/continuous_v2_1m/audit_2026-06-20/`. **Wave 1 COMPLETE.**

### Wave 2 — design note (intrabar execution engine + order/fill ledger)

Target: `liquidity_migration/trade_lifecycle.py::_simulate_indexed_trade` (the 1h
bar loop, lines ~861-951). Key design (from reading it):

- Add a registered `intrabar_resolution` setting: `1h` (current behavior, baseline
  reproduction), `1m` (next-level), with `trade` deferred.
- It must be a **hybrid**, NOT a naive bar-array swap: the HIGH/LOW-based exits
  (stop, TP) get a **1m first-touch resolver** (actual stop-vs-TP order + exact
  fill ts/price from the trade-window 1m cache), while the CLOSE-based soft exits
  (mfe_giveback, breakeven, failed_fade, event_decay, rank_exit, hash_exit) stay
  on their registered (hourly) cadence. This satisfies the plan's "1m changes only
  path-dependent trades": the TP-only control stays ~unchanged (TP fills at TP
  price either way); stops (Book A) are where 1m actually bites (the current code
  resolves same-bar stop+TP as adverse-first — stop wins — which 1m can refute).
- Same-1m-bar stop+TP (still ambiguous at 1m) → ambiguity ledger, adverse-first.
- Add the X2 order/fill ledger (decision/order/fill/slippage/fee/funding/position
  rows) around entry (currently instantaneous at entry-bar close) and exits.
- Acceptance: `intrabar_resolution=1h` reproduces Wave-0 baselines byte-for-byte
  (numerical-equivalence); `1m` changes only path-dependent trades; ambiguous
  same-bucket events visible in the ledger.

**X1 result (DONE 2026-06-20):** `intrabar_engine.py` built + 7 unit tests pass.
Real-data validation re-resolving 60 Phase-0 control trades/venue on the 1m cache:
bybit 61/61 reason-agree (price 98.4%), binance 62/62 (100%) — 1m reproduces the
TP-only control, so it changes only path-dependent (stop) trades, as required.
Receipt: `docs/preregistration/2026-06-20-continuous-v2-intrabar-execution-engine-construction.md`.
Next: X2 order/fill ledger driver, then **Book A (real stops/TPSL)** — the first
mining the 1h engine couldn't do. X3 cost calibration is gated on VPS demo fills.

### Book A — real stops / TPSL (CLOSED 2026-06-20, no both-venue candidate)

Receipt: `docs/preregistration/2026-06-20-continuous-v2-book-a-stops-tpsl-construction.md`.
Driver: `scripts/continuous_v2_book_a_stops.py` (re-resolves control trades on 1m
under A1 immediate + A2 delayed-arm stops + A7 hash null; realized-PnL MAR proxy).

**Verdict: stops do not help the fade book, even at 1m fidelity.** Control MAR proxy
bybit 6.15 / binance 4.32 (recon 0.00002/0.00000). A1 immediate stops lose 2–5 MAR
on both venues, beat no hash null, worsen drawdown. A2 delayed-arm only helps by
almost never firing; the single positive cell (bybit `stop15_arm12h` +0.75 MAR) is
−0.04 on binance → venue split, killed by the both-venue rule (like the F2 TP12
Bybit lead — operator-gated venue-policy item at most, not a frozen-object change).
Mechanism: the fade's adverse excursion is exactly where reversion to the −12% TP is
most likely, so stopping there cuts the edge; exiting AT the adverse excursion is no
better than a random-time hash null. Engine gained a byte-identical
`stop_arm_after_ms` (A2), unit-tested. The exit side is not where this book's edge
lives — consistent with the prior 1h exit-timing closure, now confirmed at 1m.
**Next mining:** Book C (TWAP/execution-impact, needs the X2 fill/cost layer) or
Book E (dynamic TP, uses this engine); entry/flow Books B/F/H need Wave-3 almanac.

### Book B — entry admission via 1m pre-entry features (CLOSED 2026-06-20)

Receipt: `docs/preregistration/2026-06-20-continuous-v2-book-b-admission-construction.md`.
Driver: `scripts/continuous_v2_book_b_admission.py`. Done as a focused Wave-3
"1m path feature" slice (causal pre-entry features), not a full almanac re-build.

**Verdict: the strongest signal in the program — but venue-split, no candidate.**
`upper_wick` (pre-entry upper-wick fraction = exhaustion) has a real both-venue IC
(+0.10 bybit / +0.12 binance), beats its hash null, and uses fresh pre-entry info
(live IC > delayed-copy). But admission (cut half) loses to the diversified control
on both venues; sizing (mean-1 tilt) beats hash on both venues and beats control on
**Binance** only (Bybit slightly worse) → venue split, killed by the both-venue rule.
The Binance-only 1m-exhaustion sizing tilt is an operator-gated venue-policy lead
(symmetric to, opposite-venue from, the Bybit-only TP12 exit lead). Confirms the
program's recurring truth — real but diffuse / venue-split — now with the best signal
found and a proper hash + delayed-copy + both-venue falsifier set. **Next:** Book C
(TWAP execution) or Book E (dynamic TP), both on the existing 1m engine.

## Open risks / honest caveats

- Full-universe hourly backtests are heavy (~6 GB RAM, minutes–tens-of-minutes
  per component build); all heavy runs go background + `--resume`, one at a time.
- Any path-dependent book (stops/TPSL, TWAP, intrabar exits) will still face the
  Bybit-vs-Binance venue-split risk until Binance 1m exists AND both venues show
  consistent path behavior; the plan's default verdict on a venue split is "no
  two-venue candidate."
- The plan's stop conditions are live: if 1m roots fail PIT/lifecycle/checksum,
  or `intrabar_resolution=1h` cannot reproduce the 1h control, stop and write a
  verdict.

## Artifact index (this program)

- Plan: `…/2026-06-19-continuous-v2-next-level-ab-research-plan.md`
- Phase 0 receipt: `…/2026-06-19-continuous-v2-phase0-baseline-construction.md`
- This log: `…/2026-06-19-continuous-v2-next-level-progress-log.md`
- Run root (data, not committed): `backtest-runs/continuous_v2_phase0_freeze_2026-06-19/`
