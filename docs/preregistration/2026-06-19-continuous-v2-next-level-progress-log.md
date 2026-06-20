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
| 1 | Full 1m PIT data foundation (Bybit + Binance) | feasibility CONFIRMED (1m download+checksum verified); build pending |
| 2 | 1m / trade-aware execution engine + order-fill ledger | not started |
| 3 | Feature Almanac V3 (`data_available_ts`) | not started |
| 4 | First A/B wave (≤1 stops/TPSL, ≤1 TWAP, ≤1 regime/vol) | gated on Waves 1–3 |
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
