# Untouched-slice provenance — Binance [2020-01-01, 2021-05-01), Bybit [2021-01-01, 2021-05-01)

Registered 2026-07-20 (tail-risk program P0.2). This note states, with
receipts, exactly how untouched each slice actually is, and freezes the
grading windows that all later reads of these slices must use. It gates every
future grading read of these ranges: a read outside the frozen windows, or
before the graded config's commit, is Lane-1 by construction.

**Headline: the proposal's D1 claim ("no generation touched them") is wrong
for Binance-2020 and misleading for Bybit.** The verification found one real
historical outcome-read and full feature exposure of the Bybit slice. The
only fully pristine range is **Binance [2021-01-01, 2021-05-01)**.

## Method

1. **Lookback derivation from code** (not assumption): every trailing window
   in the deployed/current feature stacks, from
   `liquidity_migration/continuous_events.py`, `long_native.py`,
   `risk_model.py`, `residual_momentum.py`, `continuous_btc_risk.py`.
2. **Committed-artifact sweep**: every manifest/window declaration under
   `reports/` and `docs/` (V2 discovery month manifests, V3 shared caches,
   T-A..T-K, benchmark refresh, runtime-parity epoch).
3. **Git-history archaeology** including deleted scripts and receipts:
   the V1-era OOS roots (`scripts/build_oos_roots.sh`, commit `90c5a84`),
   the tri-root protocol (`docs/tri_root_protocol.md` at `4e2d943`,
   `scripts/tri_root_creative_gate.py` at `16cd589`), round2 sweep windows
   (`e45ded7`: 2023-04-01 → 2026-05-28), and the June continuous-v2 "OOS"
   screens (early/late splits of 2023+ events, e.g. `83fef13`).

## Trailing-lookback derivation (current specs, binding values)

| Feature chain | Source | Depth |
| --- | --- | --- |
| LONG `turnover_median_90d` (min_samples=90) | `long_native.py:638-643` | **90 d (binding)** |
| LONG vol estimate / regime SMA / ATR | `long_native.py:631,695,670` | 30 d / 30 d / 14 d |
| CONT `vov` = std720h of `rv_168h` | `continuous_events.py:344,355` | 888 h ≈ 37 d |
| CONT `min720/max720`, `prior168_*` | `continuous_events.py:351-370` | 30 d / 7 d |
| BTC trend gate (prior-day 30 d sum) | `continuous_events.py:722` | 30 d + 1 |
| RMOM = Σ residual[D−9..D−3]; exposures: btc_beta 60 d cal. (builder reads start−90 d), xs_rank_ret_30d, vol 7 d | `residual_momentum.py:14-17`, `risk_model.py:48,155,190` | ≈ 70 d causal, ≤ **99 d** as implemented |
| BTC-risk score (4 components, expanding percentiles, warm-up 50 d) | `continuous_btc_risk.py:298-370` | 31 d context + 50 d warm-up |

**L_max ≈ 90–99 days.** Any bar within ~99 days before an evaluated decision
can influence it as a feature input.

## Findings

### Bybit [2021-01-01, 2021-05-01) — outcome-unread, feature-touched in full, tiny

- **Outcome-unread: confirmed.** No run — committed or recovered from git
  history — ever graded outcomes there. V2 discovery `source_window`s begin
  2021-05-01; the V1 tri-root Bybit OOS window was calendar-2022 (on the
  deleted `bybit_oos_pre2023` root); round2 graded 2023-04+; the June
  continuous-v2 "OOS" screens were early/late splits of 2023+ events;
  benchmarks start 2023-07.
- **Feature-touched: the entire slice.** The V2 discovery `month=2021-05`
  manifest records `read_window {2021-01-01, 2021-06-05}` — the slice was
  read end-to-end as feature warm-up input — and every trailing chain above
  reaches into it from the discovery window's first ~99 days regardless.
  There is **no feature-untouched Bybit subrange**.
- **Data reality:** 5 → 8 listed USDT perps (majors) across the slice; the
  root's archive begins 2021-01-01. In-slice feature warm-up consumes most
  of it: CONTINUOUS event features become well-defined ~2021-02-07+, LONG's
  90 d turnover median not until ~2021-04-01.

### Binance [2020-01-01, 2021-01-01) — OUTCOME-READ by a dead family

The V1-era momentum-factor program graded this window as its
`binance_OOS_2020` tri-root gate (receipt recovered at `4e2d943`,
`docs/tri_root_protocol.md`: baseline presets `lo_skip0` Sharpe 5.68 ✓ and
`lo_sharpe3_robust` 6.38/n=88 recorded on 2026-05-24, plus whatever
structural hypotheses `tri_root_creative_gate.py` ran; its JSON output and
the `binance_oos_pit` root were purged in the 2026-05-26/27 kill-shelve
reset). That family is dead (kill/shelve verdict `826bc78`) and is **not an
ancestor** of either deployed sleeve or of the R1/R3 candidates. The range
is therefore *not pristine*: one prior program consumed a forward-style read
of it. Numbers graded here carry that caveat permanently.

### Binance [2021-01-01, 2021-05-01) — PRISTINE

No outcome read and no feature read by any program, live or dead: the
tri-root Binance window ended 2021-01-01 (and trailing windows only look
backward); V2 was Bybit-embargoed; all Binance engine panels start
2023-04-01; benchmarks start 2023-07-16; round2 started 2023-04-01.
Universe: 80 → 111 listed USDT perps. **This is the one genuinely unopened
historical surface outside the reserved V2 label-level holdout.**

## Frozen grading windows (the gate)

Grading reads of these slices are valid only for configs **committed before
the read**, only through a committed script, with the opening recorded in
`docs/preregistration/INDEX.md`. The windows:

- **G1 (primary, pristine): Binance, entries in [2021-01-01, 2021-04-30)**
  for CONTINUOUS-shape books (24 h holds resolve ≤ 2021-05-01) and
  **[2021-01-01, 2021-04-28)** for LONG-shape books (3 d holds). Feature
  warm-up MAY read [2020-10-01, 2021-01-01) bars as inputs: warm-up is an
  input read and does not consume outcome-purity; the 2020 outcome-read by
  the dead family does not descend into R1/R3.
- **G2 (secondary, caveated): Binance, entries in [2020-04-01, 2021-01-01)**
  — the COVID-crash/DeFi-summer regime library. Every number carries the
  dead-family-read caveat; first 90 d of the root ([2020-01-01, 2020-04-01))
  serve as warm-up only, so the graded range starts 2020-04-01.
- **G3 (tertiary, weak): Bybit, entries in [2021-02-07, 2021-04-30)**
  (CONTINUOUS shape; LONG has no useful window). Universe 5–8 majors;
  outcome-unread but fully feature-touched; reported only alongside G1/G2,
  never as standalone evidence.

One read per config generation: after R1/R3 configs are graded on G1/G2/G3
once, these windows are spent for those families and revert to Lane-1
surfaces. Corrections to `docs/hypothesis_ledger.md` (the D1/"untouched"
claim) are recorded there this date.

## What this note does not claim

No statement about the reserved V2 label-level holdout (unchanged, unread).
No alpha claim. No estimate of statistical power on G1–G3 beyond the
universe counts above — effective N on 3–4 months of thin early history is
small under item-29 accounting, and any grade drawn from these windows is
regime evidence, not per-name entry evidence (proposal §4-D1's own framing).
