# Pre-registration — Continuous-Fade v2 (volatility-half-life-timed fade)

**Date:** 2026-06-03 · **Author:** continuous-halflife-fade research loop (operator-directed)
**Stage:** REJECTED (run-complete 2026-06-03; half-life-timing thesis falsified cross-venue — see Verdict)
**Design-of-record:** `docs/research/continuous_halflife_fade_research_plan.md` (this receipt freezes its §2/§8).
**Standard:** `docs/backtesting_errors_we_never_repeat.md` · **Tiers:** `STATE.md` (three-tier demo-arbiter).
**Branch:** `research/continuous-halflife-fade` (off `main`; not pushed — push auto-deploys the VPS).

This receipt is committed in the same PR as the estimator code (`liquidity_migration/continuous_halflife.py`
+ tests). All sweep runs are EXPLORATORY until a verdict is written below; only a Tier-2 pass here promotes
the sleeve to forward demo, and forward demo is the only Tier-3 arbiter (no real-money claim on backtest evidence).

## What's changing

Replace the retired always-in D9 continuous sleeve's **entry-timing layer** with a flat-by-default,
volatility-half-life-timed fade. The candidate screen reuses the SHORT system's `_select_events` chain
verbatim (PIT-correct); the new alpha is purely *when* we enter within the post-event window: short only
once realized volatility has decayed below a fixed fraction of its causal running peak. The entire
execution / accounting / cost / funding core of `continuous_events.py` is kept verbatim.

## Why this run — the two deaths v1 must engineer out

1. **~25 h look-ahead leak.** v1 built its own deciled rmom panel (`build_continuous_panel` /
   `compute_continuous_decile_panel`); the rmom join keyed on the **stamp day** (`ts_ms // MS_PER_DAY`,
   `continuous_events.py:234-235`) rather than the **trading day** `date(ts_ms − 1ms)`. Gross "+319%"
   collapsed to **+21% gross / −68% net** once made causal (commit 18e9bbd retracted that verdict). NB: the
   2026-06-01 engine audit's entry-delay latency sweep PASSED — an entry-timing test cannot catch a
   *selection-feature availability* leak. v2 abandons the rmom decile panel entirely (uses `_select_events`,
   whose `tradable_membership_flag` keys on the trading day), so this leak is gone by construction.
2. **Structural cost trap.** v1 was always-in (~11.8k trades); `36 bps × 11.8k` buried any thin per-trade
   edge. v2 converts an event to a trade only once vol decays in-window → trade count must drop ~10–50×.

## Hypothesis (frozen before any run)

A liquidity-migration pump injects transient volatility that decays at a measurable per-name rate. Entering
the fade at the top captures mean-reversion but also the highest residual upside variance (the catch-the-top
failure). Entering once realized vol has decayed below a fixed fraction of its (causal, running) event-peak —
roughly one half-life — waits for the move to exhaust, so the short is the confirmed giveback rather than the
pop. Because entry is sparse and well-timed, the realistic ~26–36 bps round-trip clears where the always-in
design's did not.

## Predicted direction + magnitude (pre-committed, falsifiable)

- **Net return positive on BOTH venues** (`bybit_full_pit` AND `binance_full_pit`) after the full
  cost+funding model.
- **Trade count an order of magnitude below ~11.8k** — target low-hundreds to ~1–2k over the full window.
  (If it still fires ~10k trades, "flat by default" is not implemented — itself a falsifier.)
- **Cost drag materially below v1's −65%; funding drag below −25%.**
- **Edge monotone-ish in decayed-RV** on the in-sample diagnostic: decayed-RV entries outperform
  still-hot / slow-decay entries. If decayed and non-decayed perform the same, the half-life adds nothing.
- **Believable economics:** median net per-trade > 0, hit-rate ~45–55%, losing months PRESENT, drawdown
  well into double digits (a regime-conditional short book is not sub-5%-DD or monotone-up).

## Falsifiers / kill criteria (any one → `invalid`/`rejected`, stop)

- Edge exists only at `entry_delay_hours=0`, or collapses under the **+1-bar delayed-copy latency test**
  (every half-life/RV input shifted +1 extra bar) → look-ahead, the old disaster recurring.
- Net-negative on **either** venue after full costs+funding.
- **Sign flip** bybit vs binance → venue microstructure, not alpha.
- Trade count stays ~10k / `skipped_capacity` not near zero (slots saturated) → the old cost trap rewritten.
- The half-life parameter has **no monotone relationship** to fade PnL → the timing element is noise; any
  positive result is the underlying fade (selection + decel), and the v2 thesis is FALSIFIED even if returns
  are positive (report as such; do not relabel as a different finding — honesty rule).
- A "too good" result (36/36-up, sub-5%-DD, monotone equity) with the leak hunt not exhausted → BLOCKED until
  explained (RED-FLAG protocol §8.3 of the plan).
- Cache-staleness / non-full-PIT / partial-PIT in the run record → `invalid`, re-run.
- A verdict computed on an exploratory (non-pre-registered) run → may not be cited as evidence.

## Half-life candidate set (FROZEN up front — the multiple-testing surface)

The estimator search is inherently multiple-testing, so the full candidate set is frozen here and **every
cell's full distribution is reported, not just the winner**.

- **H-default = method (e):** non-parametric "RV ≤ c·running-peak" crossing (model-free; lowest leakage; two
  params; `c=0.5` ⇔ wait one half-life). **The headline trigger.**
- **H-confirm = method (a):** OU/AR(1) half-life on a trailing-window RV series — exact `−ln2/ln(1+β̂)` form;
  gates `φ̂∈(0,1)`, `|t(β̂)|≥t_min`, `Ĥ≤H_max`. Used first as a *confirmer of* (e) (enter only when both
  agree), then standalone.
- **H-fallback = method (b):** exponential log-OLS decay of post-(running)peak RV; gate on significantly
  negative slope AND `τ̂·ln2 ≤ H_max`.
- **RV generator = method (d) EWMA** (RiskMetrics, strictly causal) — feeds (e)/(a)/(b); seeded with the
  trailing-window variance, never the full sample.
- **Cross-checks / characterizers only (never a primary trigger):** method (c) ACF-of-|returns| (noisiest);
  method (d) GARCH long-window baseline persistence as a *reject* filter.

**Headline pre-registered cell: (e) as the gate, (a) as the confirmer.** Live-parity constraint (binding):
every estimator must be expressible as a trailing per-symbol window so it reproduces byte-equivalent in
`continuous_demo.py` via `LivePanelCache`; any estimator that cannot is rejected at design time.

## Parameters to sweep (FROZEN)

| Param | Method | Grid | Notes |
|---|---|---|---|
| `RV` proxy | all | {Parkinson (default), Garman-Klass, rolling-std(6h), EWMA} | one choice, fixed within a run |
| `vol_decay_frac` (c) | (e),(b) | {0.3, 0.4, 0.5, 0.6, 0.7} | ⇔ # half-lives waited (= the core gate) |
| `d_min` (bars since peak) | (e) | {3, 6, 12, 24} h | floor on elapsed time since running peak |
| debounce `k` | (e) | {1, 2, 3} | consecutive bars below c·peak (noise guard) |
| `t_min_bars` (min bars post-event) | all | {3, 6, 12} h | no entry on tiny samples |
| `max_observe_hours` | entry | {48} (fixed S1; {72} robustness) | watch window per event |
| `L` (trailing fit window) | (a),(b),(EWMA seed) | {48, 72, 96, 168} h | stationarity vs responsiveness |
| `H_max` (max half-life accepted) | (a),(b) | {6, 12, 24, 48} h | "fast enough decay" |
| `t_min` (slope/β significance) | (a),(b) | {1.5, 2.0, 2.5} | reject noise fits |
| `λ` (EWMA) | (d) | {0.94, 0.97, 0.99} | ⇔ H ≈ {11,23,69} bars |
| GARCH baseline window | (d) | {30, 60, 90} d | reject-filter only |
| since-event vs trailing | (a) | {trailing (default), blend} | expanding biased-long, guarded |
| time-stop `hold_hours` | exit | {24, 48, 72} h | cost/funding bite with hold |
| `tp_frac` (fade take-profit) | exit | {0 (off), 0.3, 0.5} | × pump amplitude |
| `vol_reaccel_mult` | exit | {0 (off), 2.0, 3.0} | × entry vol (re-acceleration stop) |
| `entry_decel` (lookback,max_ret) | entry | {off, (6h, 0.0)} | confirmed-fade decel gate (existing `_run_trades`) |
| `liq_turnover_min` | screen | {500k, 1M, 2M} | impact vs sample |
| sizing | sizing | {flat, inverse_vol} | inverse_vol default per §5.3 |

**Pre-declared CONTROL (no-half-life baseline):** enter-immediately-on-event (the half-life gate disabled;
entry at `t_min_bars` floor only), same selection / cost / exit / sizing. The Tier-2 MAR-Δ is measured
**vs this control** — it isolates the *timing* contribution from the underlying fade.

**Staged execution (avoids an all-or-nothing grid — error #25):**
- **Stage 1 (core):** method (e), Parkinson RV, sweep {vol_decay_frac × d_min × k × t_min_bars × hold ×
  liq × sizing} + the CONTROL, both venues, **train segment only**. Map the response surface + the
  monotone-in-decayed-RV diagnostic. Per-cell ledgers checkpointed to disk.
- **Stage 2 (robustness, on the Stage-1 winner neighborhood only):** RV-proxy variation; the (a) OU
  confirmer and (b) fallback; the cost-stress ladder (1× / 1.5× / 2× fees+slippage); exit knobs
  (tp_frac, vol_reaccel_mult).
- **Stage 3 (lock):** confirm the locked winner on the **held-out OOS segment** (untouched until lock) +
  cross-venue sign + the §8.4 thresholds.

## Roots that will be touched

- [x] `~/SHARED_DATA/bybit_full_pit` (per-venue working dataset) — full PIT, klines end ~2026-05-27.
- [x] `~/SHARED_DATA/binance_full_pit` (per-venue working dataset) — full PIT.
- [x] forward demo/paper — only if Tier-2 passes (operator-gated; the Tier-3 arbiter).

**Train / OOS split (pre-committed, time-ordered — no internal pre-2023 OOS exists):**
- **TRAIN:** `start` (2023-04-01) → 2025-06-01 (~26 mo; the engine's existing `split_date` early era).
- **OOS-holdout:** 2025-06-01 → 2026-05-28 (~12 mo; the recent era). Untouched for selection until the
  winner is locked; the moment it influences a threshold it is no longer OOS.
- **Pristine OOS = forward demo/paper** (Tier-3, out of scope here).
Winner is selected ONLY on TRAIN-segment metrics; OOS-holdout metrics are read only after lock.

## Causal / no-look-ahead obligations (gate everything before any sweep)

The causality unit tests must pass BEFORE any sweep (plan §8.3.1, §10.3). Each half-life-specific leak trap
gets a test that fails loudly:
1. **Truncation invariance:** `estimate_vol_halflife(...)` / the entry trigger computed on bars truncated at
   `t` is identical (`np.allclose`) to the value on the full series; appending bars `>t` does not change it.
2. **Running-peak causality:** `RV_peak(t) = max_{i≤t} RV_i` (never the hindsight max).
3. **Per-symbol independence:** RV is the symbol's own trailing window (`.over("symbol")`), never a
   within-ts cross-section.
4. **+1-bar delayed-copy latency test (the decisive one):** re-run with every half-life/RV input shifted +1
   extra bar; the edge must degrade *gracefully*, not cliff. Survives-only-at-0-lag → reject.
5. **State-init parity:** exits/stops/cooldown initialize at activation (warm-start ban), matching
   `continuous_demo`.
6. **Honest fill:** trigger on a closed bar `t`; order submitted at `t + entry_delay_hours` (≥1);
   `entry_delay_hours=0` is the look-ahead proxy and is labelled `invalid`.

## Cache-freshness discipline (mandatory before EVERY validation — §8.5)

A stale panel cache already produced an invalid verdict here. Before every (re-)validation:
1. **Hard-clear engine + half-life caches:** `rm -f <root>/_continuous_engine_panel_*.parquet
   <root>/_continuous_halflife_*.parquet`. The half-life cache key/staleness check is extended to cover the
   estimator's config + code hash (the `_panel_cache_stale` mtime guard keys only on the rmom file, so a
   code-only change is otherwise served stale).
2. Verify the run label (`full_pit_universe_pass=True`, `run_label=full_pit_universe` / EXPLORATORY) and
   data-root identity before trusting any number.
3. A verdict on a cache whose mtime predates the latest rmom/klines refresh is `invalid` — re-run.

## Decision rule (a priori, BINDING — Tier-2 demo-candidate, matches `scripts/r1_robustness.py`)

ACCEPT to demo-candidate only if ALL hold:
- `full_pit_universe_pass = True` on **both** venues.
- **Net return positive on BOTH venues** after the full cost+funding model.
- **Pooled MAR Δ > +0.10** vs the no-half-life CONTROL, AND neither venue worse than **−0.5 MAR**.
- Trade-count floor: **≥30 bybit / ≥20 binance**, AND order-of-magnitude < ~11.8k (thesis fidelity).
- Edge survives the cost-stress ladder (still net-positive both venues at ≥1.5× costs).
- Smell-test passes (losing months, believable DD, no monotone-up curve).
- R1 robustness: positive in a majority of thirds; LOO-month does NOT flip the sign; bootstrap MAR-Δ p5
  not deeply negative; top-3-month concentration not carrying ~all the edge (reported; non-blocking at T2).

**Tier-3 (real money) is OUT OF SCOPE** (forward-demo arbiter; the account stays on demo; `REAL_MONEY` not
toggled). If the binding rule is not met, the verdict is `rejected` and the thesis is killed — not rationalized.

## Run command (intended — committed with the code)

```bash
# Clear caches first (cache-freshness discipline above), then the staged sweep on both venues.
for r in ~/SHARED_DATA/bybit_full_pit ~/SHARED_DATA/binance_full_pit; do
  rm -f "$r"/_continuous_engine_panel_*.parquet "$r"/_continuous_halflife_*.parquet
done
POLARS_MAX_THREADS=8 .venv/bin/python scripts/continuous_halflife_sweep.py \
  --roots ~/SHARED_DATA/bybit_full_pit ~/SHARED_DATA/binance_full_pit \
  --stage 1 --segment train --out ~/SHARED_DATA/cont_halflife/
```
(The exact sweep driver `scripts/continuous_halflife_sweep.py` is committed with the estimator; it
parallelizes over cells on the 32-thread box, checkpoints per-cell ledgers, and writes a grid summary +
the full per-cell distribution. The single-cell path is `run_continuous_event_research(root,
config=HalflifeFadeConfig(...))`.)

## Post-run results (2026-06-03, EXPLORATORY engine runs; pre-reg frozen before the runs)

Code landed on branch `research/continuous-halflife-fade` (uncommitted): `liquidity_migration/continuous_halflife.py`
(causal estimators + §3 candidate screen + §5 entries + runner), `tests/test_continuous_halflife.py` (23
causality/correctness tests), this receipt. Stage-1 core grid + the pre-declared CONTROL were run on both
venues; the kill criteria fired on the headline (e) estimator + control + cross-venue, so the full §8.2 grid
and Stage-2 (RV-proxy variants, OU/exp estimators) were NOT run — sweeping a falsified thesis would be the
"rationalize a failed thesis" trap the standard forbids.

**Causality — PASS (no look-ahead).** 23 estimator unit tests pass (truncation/append/future-mutation
invariance). Engine-level +1-bar latency falsifier on bybit (the decisive test): the edge does NOT peak at
the look-ahead proxy (`entry_delay_hours=0`) and does NOT collapse under a +1-bar RV input lag — it is
flat-to-stronger as both delays rise (delay 0→1→2→3h ret 3.44→4.18→4.23→3.79%; RV input-lag 0→1 ret
4.18→6.33%). A v1-class leak would do the opposite. So any signal here is a real multi-hour process, not an
artifact. (`build_event_candidates`: bybit 613 events, binance 314; full-PIT pass both.)

**The half-life TIMING adds no value — the pre-registered discriminator (CONTROL = enter-immediately-on-event):**

| cell (method (e), Parkinson, liq $500k, flat, hold 12h unless noted) | bybit n / ret% / MAR | binance n / ret% / MAR |
|---|---|---|
| **CONTROL (no half-life gate)** | **242 / +9.59 / 1.76** | **233 / −3.18 / −0.16** |
| c=0.4 | 77 / +3.02 / 0.67 | 130 / +1.04 / 0.39 |
| c=0.5 (default) | 121 / +4.18 / 0.49 | 170 / +0.63 / 0.14 |
| c=0.6 | 151 / +0.14 / 0.01 | 193 / +1.23 / 0.35 |
| c=0.7 | 173 / +3.35 / 0.34 | 210 / +0.45 / 0.10 |
| d_min=12 | 118 / +6.36 / 2.09 | 158 / +2.49 / 0.85 |
| hold=48 | 120 / +8.82 / 1.53 | 169 / +4.72 / 0.70 |

Findings: (1) **`c` (# half-lives waited) has NO monotone relationship to PnL on either venue** — bybit
{0.3..0.7}→{0.32,3.02,4.18,0.14,3.35}%, binance flat ~0.5–1.2% (noise). (2) **The gate's effect vs the
no-timing control flips sign across venues** — on bybit the control DOMINATES every gated cell (MAR Δ
gate−control = 0.49−1.76 = **−1.27**, gate also worse per-trade), on binance the gate marginally beats a
negative control (+0.30 MAR) at ~0 magnitude. **Pooled MAR Δ vs control ≈ −0.49** (≪ the +0.10 Tier-2 bar;
bybit far worse than −0.5). (3) The underlying fade itself (the control) is **not cross-venue robust** — a
sign flip (+9.59% bybit, −3.18% binance at hold 12h). Artifacts: `~/SHARED_DATA/cont_halflife/smoke_bybit/`
+ the surface/falsifier logs.

**Robustness of the kill — confirmed across the FULL pre-registered estimator + RV-proxy set (2026-06-03).**
Each run once vs the control, both venues (n / ret% / MAR, c=0.5 where applicable, hold 12h):

| cell | bybit | binance |
|---|---|---|
| CONTROL (no gate) | 242 / +9.59 / 1.76 | 233 / −3.18 / −0.16 |
| (e) Parkinson | 121 / +4.18 / 0.49 | 170 / +0.63 / 0.14 |
| (e) rolling-std | 156 / +6.58 / 1.22 | 189 / −0.18 / −0.04 |
| (e) Garman-Klass | 114 / +3.44 / 0.39 | 177 / +0.31 / 0.09 |
| (b) exp standalone | 167 / +2.93 / 0.33 | 209 / +1.42 / 0.38 |
| (a) OU standalone | 53 / +1.56 / 1.01 | 86 / +2.05 / 1.23 |
| (e)+OU-confirm | 4 / +0.06 (degenerate) | 6 / +0.32 (degenerate) |
| (e) EWMA | 0 (degenerate) | 0 (degenerate) |

No estimator/proxy produces a robust cross-venue edge that beats the no-timing control: on bybit the control
DOMINATES every timing variant (the timing removes value); on binance everything is ~0/negative. The single
nominally-both-venues-positive cell, **(a) OU standalone**, is NOT a salvage — economically negligible
(~0.5–0.7%/yr on 53/86 trades), it FAILS the Tier-2 per-venue floor (bybit MAR Δ vs control = 1.01−1.76 =
**−0.75** < −0.5), and it only looks "positive both venues" because the binance control is itself negative
(the timing still destroys the bybit control's edge, 9.59%→1.56%). Reported per the full-distribution rule;
it does not meet the bar and is not cherry-picked. (e)+OU-confirm and EWMA are degenerate (≈0 entries). The
REJECT is estimator-robust.

## Verdict — REJECTED (half-life-timing thesis falsified; not promoted)

**REJECTED.** The continuous-fade v2 hypothesis — that timing the fade by a causal post-event volatility
half-life adds value over entering immediately — is falsified. Per the binding decision rule and §2/§9.1
kill criteria: the half-life parameter `c` has **no monotone relationship to fade PnL** on either venue
(timing is noise); the gate's value-add **flips sign cross-venue** and the **pooled MAR Δ vs the no-half-life
control is ≈ −0.49**, far below the +0.10 Tier-2 bar (and < −0.5 on bybit); and the underlying fade's own
return **sign-flips bybit↔binance**. This is NOT a look-ahead artifact (the +1-bar latency falsifier and the
23 causality tests pass) — it is an honest negative: v2's core innovation does not work. No promotion, no
forward demo, no live wiring (plan §10.9 not executed). The estimator module is causal and reusable, but the
thesis it serves is dead. Per the honesty rules, this verdict is binding and not to be re-purposed; the
account stays on demo. Byproduct note (not a new edge): a promoted-selection continuous fade with a fixed
short wait is strongly positive on bybit (+9.59%, MAR 1.76) but negative on binance — consistent with the
prior continuous-fade conclusion (real-but-venue-fragile / redundant with the daily short; the open question
there remains the G1 redundancy test, unrelated to this half-life thesis).
