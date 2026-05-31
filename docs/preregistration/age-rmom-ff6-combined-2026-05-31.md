# Pre-registration — daily age + residual-momentum + failed-fade combined stack

**Date:** 2026-05-31
**Run label:** VALIDATED  (per `parameter_pre_registration.md` — this result MAY be cited as evidence in a forward-demo decision)
**Author/owner:** rob435
**Status:** PLANNED (run-pending on the 5950X; the 16 GB research box cannot hold a full-PIT cell ~23 GB)

## 0. Why this run exists (the gap)

E2 validated the **age gate** (`pit-age-days-min≈300`) and P3b validated the
**residual-momentum gate** (`residual-momentum-max` at the per-venue median)
*separately, against different baselines*. R13 validated the **failed-fade exit**
(`ff6_4pct`) *on the `drop_all_4` entry population*. **The three have never been
measured as one stack.** RD1 showed age and rmom both target the *same* enemy —
the bull-market squeeze on idiosyncratically-strong young names — so whether they
**stack** (independent gains) or **overlap** (harvest the same names twice) is a
real open question, not a given. And ff6 cuts squeeze-losers *after* entry, while
rmom removes squeeze-prone names *before* entry, so ff6 may have fewer losers left
to catch on an age+rmom book. This run resolves all three margins at once.

This is the [STATE.md](../../STATE.md) "open daily lead" and supersedes the stale
K0b "age+rmom combined baseline" sketch (which lacked the ff6 layer and a control
factorial).

## 1. Hypothesis (one sentence, falsifiable)

On the same full-PIT daily event population, **age300 + rmom\@median stack to a
cross-venue MAR strictly above the better single gate**, and adding **ff6_4pct**
raises MAR further on both venues — all-weather (early **and** recent positive),
not a recent-regime artifact.

## 2. Decision rule (pre-committed — three-tier demo-arbiter, MAR-primary)

Copied from [STATE.md](../../STATE.md) "Decision rules currently binding". This run
can earn at most **Tier-2 (demo-candidate)** — it is in-sample; the forward demo is
the only Tier-3 arbiter.

**Tier-2 (demo-candidate) for the combined `age_rmom` and `age_rmom_ff6` cells vs `00_baseline`:**
- Return positive on **both** venues (direction guard)
- **Pooled** MAR Δ > +0.1 (mean of the two venue MAR deltas) vs `00_baseline`
- Neither venue worse than MAR Δ ≥ −0.5
- ≥30 Bybit / ≥20 Binance trades
- Fragility (bootstrap p5, LOO, sign-consistency) REPORTED, non-blocking

**Stacking test (the headline question), pre-committed:**
- **STACK** if `age_rmom` MAR > max(`age` MAR, `rmom` MAR) on **both** venues.
- **OVERLAP** if `age_rmom` MAR ≤ the better single gate on either venue (they
  harvest the same factor → the second gate is redundant; deploy the better single one).
- **ff6 ADDS** if `age_rmom_ff6` pooled MAR Δ > `age_rmom` pooled MAR Δ AND ff6
  improves (or is flat on) both venues (R13 pattern: bybit DD-shave, binance return-lift).

## 3. Parameters under test (frozen before the run)

Cells (each venue). ff6 changes **exit** only, so `age_rmom` and `age_rmom_ff6`
have **identical entries** — the ff6 delta is a pure exit-rule effect (R13 design #19).

| cell-id | age (`pit-age-days-min`) | rmom (`residual-momentum-max`) | ff6 (failed-fade) | role |
|---|---|---|---|---|
| `00_baseline` | off (0) | off (10.0 = inactive) | off | control |
| `age` | 300 | off | off | age-alone margin |
| `rmom` | off | per-venue median | off | rmom-alone margin |
| `age_rmom` | 300 | per-venue median | off | **the never-run combined cell** |
| `age_rmom_ff6` | 300 | per-venue median | **on** (6h / 4% / 1% mfe) | **the full stack** |

- `ff6` knobs (the deployed/`demo_relaxed`-tested `ff6_4pct`): `failed-fade-exit-hours=6`,
  `failed-fade-loss-pct=0.04`, `failed-fade-min-mfe-pct=0.01`, `failed-fade-close-location-min=0.0`.
- `rmom` gate value = the **per-venue median** of the freshly-recomputed
  `<root>/residual_momentum.parquet` (P3b medians were 0.1377 bybit / 0.1148 binance —
  **re-derive, do not hardcode**; record the fresh medians in §6).
- All other knobs = the validated `volume_events_cell.sh` defaults (full-PIT, `bar_extreme_capped`
  10%, max_active=12, 45 bps = ×3 conservative cost).

## 4. Universe / data / window

- **Data roots:** `~/SHARED_DATA/bybit_full_pit`, `~/SHARED_DATA/binance_full_pit`
  (the canonical research roots — [data_roots.md](../data_roots.md)). Full-PIT universe required.
- **Window:** 2023-04-01 → 2026-05-28 (matches E2/R13). **Early/recent split: 2025-06-01**
  (matches RD1 / the program convention) — every cell reports both sub-periods, both venues.
- **Cost / fills:** 45 bps round-trip (×3), `bar_extreme_capped` 10% stop fill.

## 5. Run command(s) (copy-pasteable; run on the 5950X)

```bash
# 0) one-time per root — precompute the PIT-clean residual-momentum signal
POLARS_MAX_THREADS=8 .venv/bin/python -u scripts/precompute_residual_momentum.py   # no --root → both full-PIT roots

# 1) read the fresh per-venue medians (record them in §6 before running the gated cells)
.venv/bin/python - <<'PY'
import polars as pl, pathlib
for v in ("bybit","binance"):
    p = pathlib.Path.home()/ "SHARED_DATA" / f"{v}_full_pit" / "residual_momentum.parquet"
    m = pl.read_parquet(p)["residual_momentum"].median()
    print(f"{v}: median residual_momentum = {m:.4f}")
PY

# 2) the five cells per venue (run full-PIT serial; ~23 GB/cell → SWEEP_MAX_WORKERS=1)
#    set RMOM_BY / RMOM_BN to the medians printed above.
RMOM_BY=0.1377   # <-- replace with fresh bybit median
RMOM_BN=0.1148   # <-- replace with fresh binance median
TAG=age_rmom_ff6_2026-05-31

for V in bybit binance; do
  RM=$([ "$V" = bybit ] && echo "$RMOM_BY" || echo "$RMOM_BN")
  SWEEP_MAX_WORKERS=1 POLARS_MAX_THREADS=8 bash scripts/volume_events_cell.sh --venue "$V" \
    --cell-id 00_baseline --phase "$TAG" --overrides ''
  SWEEP_MAX_WORKERS=1 POLARS_MAX_THREADS=8 bash scripts/volume_events_cell.sh --venue "$V" \
    --cell-id age --phase "$TAG" \
    --overrides 'liquidity-migration-pit-age-days-min=300'
  SWEEP_MAX_WORKERS=1 POLARS_MAX_THREADS=8 bash scripts/volume_events_cell.sh --venue "$V" \
    --cell-id rmom --phase "$TAG" \
    --overrides "liquidity-migration-residual-momentum-max=$RM"
  SWEEP_MAX_WORKERS=1 POLARS_MAX_THREADS=8 bash scripts/volume_events_cell.sh --venue "$V" \
    --cell-id age_rmom --phase "$TAG" \
    --overrides "liquidity-migration-pit-age-days-min=300,liquidity-migration-residual-momentum-max=$RM"
  SWEEP_MAX_WORKERS=1 POLARS_MAX_THREADS=8 bash scripts/volume_events_cell.sh --venue "$V" \
    --cell-id age_rmom_ff6 --phase "$TAG" \
    --overrides "liquidity-migration-pit-age-days-min=300,liquidity-migration-residual-momentum-max=$RM,failed-fade-exit-hours=6,failed-fade-loss-pct=0.04,failed-fade-min-mfe-pct=0.01,failed-fade-close-location-min=0.0"
done

# 3) verdict + fragility (Tier-2) against the control
.venv/bin/python scripts/r1_robustness.py --sweep-tag "$TAG" --control 00_baseline
```

> Confirm every cell logs `run_label='full_pit_universe'` (not `INVALID (partial-PIT)`)
> before trusting any number — the partial-PIT survivorship trap (see the corrected memory).

## 6. What gets reported (committed before seeing results)

A results table appended to this receipt, per venue × per cell:
- **trade count** (n_bybit / n_binance) — *explicitly*, since the question includes
  "does it cut trade count?" Report `00_baseline` n and the % cut for each gated cell.
- Return (×), max-DD, **MAR**, Sharpe; **all per early/recent third** (both venues).
- Exit-reason histogram for `age_rmom` vs `age_rmom_ff6` (how many trades ff6 actually catches).
- The fresh per-venue rmom medians used.
- `r1_robustness.py` Tier-2 verdict + bootstrap p5 / LOO / sign-consistency per gated cell.
- The three pre-committed verdicts: STACK-vs-OVERLAP, and ff6-ADDS yes/no.

## 7. Falsifier / kill criteria

- **No stacking:** `age_rmom` MAR ≤ the better single gate on either venue → file
  "age & rmom overlap (same factor); deploy the single better gate, not the stack."
- **Recent-only:** the combined lift is positive recent but negative early on either
  venue → regime bet, not all-weather (the c2b trap) → not a Tier-2 demo-candidate.
- **ff6 hurts:** `age_rmom_ff6` pooled MAR Δ < `age_rmom` OR ff6 turns a venue negative
  → drop ff6 from the stack (consistent with R13's "fragility p5 slightly negative" caveat).
- **Direction/size guards:** return negative on either venue, or <30 by / <20 bn trades
  on any gated cell → fails Tier-2 outright.

## 8. Provenance

- Engine gates: age `volume_events_filters.py:756`, rmom `volume_events_filters.py:762`
  (keep LOW rmom + drop nulls), ff6 `volume_events.py:1539` (`_failed_fade_exit_hit`).
- Prior single-margin evidence: E2 (age) + P3b (rmom) + R13 (ff6 on drop_all_4) — all in
  [research_summary.md](../research_summary.md) / git history.
- Run logs + per-cell summary CSVs + per-trade ledgers under the sweep tag (commit the
  summary CSV + this completed receipt in the same PR per the pre-registration standard).

---

## RESULTS (filled 2026-05-31, post-run — §1–8 above are the pre-committed plan, untouched)

**Run:** tag `age_rmom_ff6_2026-05-31`, 5 cells × 2 venues, all 10 cells
`run_label='full_pit_universe'` (full-PIT verified per cell; the engine hard-aborts
partial-PIT and none did). Box: 5950X, serial `SWEEP_MAX_WORKERS=1 POLARS_MAX_THREADS=8`.
Summary CSV: `age-rmom-ff6-combined-2026-05-31_summary.csv` (this dir).

### Spec corrections applied (operator-confirmed before the run)

The receipt's §5 copy-paste commands omitted three knobs that §3/§4 specify; running them
literally would have used the wrapper defaults and contradicted the receipt's own scientific
spec. Operator confirmed: **honor §3/§4.**
- **Window:** added `--start 2023-04-01 --end 2026-05-28` (§4; §5 omitted ⇒ wrong 2025-01-01 default).
- **Concurrency:** `max-active-symbols=12` on all cells (§3; §5 omitted ⇒ wrong default 3).
- **Baseline age:** `00_baseline` and `rmom` cells run `pit-age-days-min=0` (§3 table "off (0)";
  §5 left the wrapper default 90). The age×rmom factorial is therefore measured from a true
  no-age-floor control.

### Fresh per-venue rmom medians (re-derived; the stale 0.1377/0.1148 were NOT used)

| venue | fresh median `residual_momentum` | parquet rows |
|---|---|---|
| bybit | **−0.012738** | 445,985 |
| binance | **−0.010021** | 406,002 |

A 7-day sum of mean-zero cross-sectional residuals has a ~0 median, so the old positive
~0.13 values barely gated. At the fresh median the gate is at the true ~50th percentile of
the *panel* — but event candidates are freshly-*pumped* names (high residual momentum), so
most fall **above** the median and are dropped ⇒ the rmom gate is far more aggressive on the
event population than P3b's stale-median version (see trade-count collapse below).

### Trade count + % cut vs `00_baseline`  (the question's explicit "does it cut trade count")

| cell | bybit n | bybit %cut | binance n | binance %cut |
|---|---|---|---|---|
| `00_baseline` | 798 | — | 519 | — |
| `age` | 579 | 27.4% | 307 | 40.8% |
| `rmom` | 69 | **91.4%** | 55 | **89.4%** |
| `age_rmom` | 39 | **95.1%** | 24 | **95.4%** |
| `age_rmom_ff6` | 39 | 95.1% | 24 | 95.4% |

The fresh-median rmom gate is brutal: it alone cuts ~90% of trades. `age_rmom` thins the
3-year book to **39 bybit / 24 binance** — above the Tier-2 floors (≥30/≥20) but only just,
and a near-degenerate ~13 trades/yr.

### Full-window metrics (engine daily-resolution DD; MAR = ann-ret / |DD| over the true 3.16y span)

Bybit (funding = real, per-trade; mostly fully-covered):

| cell | ret | DD | MAR | Sharpe |
|---|---|---|---|---|
| `00_baseline` | +0.31× | −24.0% | 0.37 | 0.58 |
| `age` | +0.71× | −17.7% | 1.05 | 1.28 |
| `rmom` | +0.36× | −3.3% | **3.09** | 2.40 |
| `age_rmom` | +0.17× | −3.6% | 1.38 | 2.34 |
| `age_rmom_ff6` | +0.17× | −3.6% | 1.38 | 2.34 |

Binance (funding **missing** on all cells ⇒ funding-blind, optimistic for a short):

| cell | ret | DD | MAR | Sharpe |
|---|---|---|---|---|
| `00_baseline` | −0.17× | −42.8% | −0.14 | −0.38 |
| `age` | +0.22× | −12.6% | 0.53 | 0.71 |
| `rmom` | +0.37× | −1.7% | **6.33** | 2.88 |
| `age_rmom` | +0.18× | −1.2% | 4.40 | 1.71 |
| `age_rmom_ff6` | +0.18× | −1.2% | 4.40 | 1.71 |

**Caveat on the high `rmom`/`age_rmom` MARs:** they ride on near-zero DDs (−1 to −4%) over a
thin book, so MAR is a divide-by-small-number that overstates risk-adjusted quality. Binance
early-third MAR is literally undefined (~0 DD). The DD collapse — not a return jump — drives
the MAR (rmom return ≈ baseline return; the gate just removes the drawdown-causing trades).

### Early/recent split (2025-06-01, receipt §4; per-sub-period annualized MAR)

| venue / cell | early ret | early MAR | recent ret | recent MAR |
|---|---|---|---|---|
| by `00_baseline` | +0.53× | 1.51 | −0.13× | −0.71 |
| by `age` | +0.49× | 1.75 | **+0.16×** | **1.11** |
| by `rmom` | +0.07× | 1.20 | +0.27× | 19.2 (≈0-DD) |
| by `age_rmom` | +0.05× | 2.63 | +0.11× | 8.1 (≈0-DD) |
| bn `00_baseline` | +0.38× | 1.85 | −0.39× | −1.07 |
| bn `age` | +0.23× | 2.58 | −0.01× | −0.10 |
| bn `rmom` | +0.03× | nan(≈0-DD) | +0.33× | 32.6 (≈0-DD) |
| bn `age_rmom` | +0.03× | nan(≈0-DD) | +0.14× | 12.7 (≈0-DD) |

All gated cells are **positive in BOTH sub-periods** (not the c2b recent-only trap) **except
`age` on binance** (recent −1%, marginally negative — but a huge lift off the baseline's −39%).
`rmom`/`age_rmom` are **recent-tilted**: early returns are near-flat (+0.03 to +0.07×); most of
the edge is recent. Their early sub-period passes the sign test but is thin.

### Exit histogram — `age_rmom` vs `age_rmom_ff6` (how many trades ff6 catches)

| exit reason | bybit `age_rmom` | bybit `age_rmom_ff6` | binance `age_rmom` | binance `age_rmom_ff6` |
|---|---|---|---|---|
| `exit_event_decay` | 28 | 28 | 18 | 18 |
| `exit_max_hold` | 1 | 1 | 0 | 0 |
| `exit_stop_loss` | 6 | 6 | 2 | 2 |
| `exit_take_profit` | 4 | 4 | 4 | 4 |
| **`exit_failed_fade`** | **0** | **0** | **0** | **0** |
| TOTAL | 39 | 39 | 24 | 24 |

**ff6 catches ZERO trades.** `age_rmom_ff6` is byte-identical to `age_rmom` on both venues
(same trades, returns, DD, MAR, exits). Exactly as the receipt hypothesized: rmom removes the
squeeze-prone names *before* entry, leaving ff6 no failing fades to cut *after* entry.

### r1_robustness Tier-2 verdict + fragility (`scripts/r1_robustness.py --control 00_baseline`)

| cell | by MARΔ | bn MARΔ | pooled MARΔ | by/bn ret | by/bn trades | verdict |
|---|---|---|---|---|---|---|
| `age` | +0.68 | +0.67 | +0.67 | +0.7×/+0.2× | 579/307 | DEMO-ELIGIBLE |
| `rmom` | +2.71 | +6.47 | +4.59 | +0.4×/+0.4× | 69/55 | DEMO-ELIGIBLE |
| `age_rmom` | +1.01 | +4.54 | +2.77 | +0.2×/+0.2× | 39/24 | DEMO-ELIGIBLE |
| `age_rmom_ff6` | +1.01 | +4.54 | +2.77 | +0.2×/+0.2× | 39/24 | DEMO-ELIGIBLE |

Fragility (REPORTED, non-blocking at Tier-2): all four cells **LOO sign-stable** (no third
flips the sign). Bootstrap MAR-Δ P(Δ>0): `age` 91/97%, `rmom` 91/81%, `age_rmom` 100/89%.
Bootstrap **ann-return**-Δ p5 is the honest stress: `age` −1.8%/−1.0%, `rmom` −1.4%/+37.8%,
`age_rmom` +1.7%/+12.7% — i.e. the *return* edge is not bulletproof at p5 on bybit for the
single gates, but the gated cells survive. `age` bybit is all-thirds-positive; `age` binance
recent third is marginally negative.

### THE THREE PRE-COMMITTED VERDICTS (§2 rules)

1. **STACK vs OVERLAP → `OVERLAP`.** Rule: STACK iff `age_rmom` MAR > max(`age`,`rmom`) on
   BOTH venues. Bybit `age_rmom` 1.38 < `rmom` 3.09; binance `age_rmom` 4.40 < `rmom` 6.33.
   `age_rmom` MAR is **below the better single gate on both venues** ⇒ the two gates **overlap**
   — they harvest the same factor (RD1: both target the bull-squeeze on idiosyncratically-strong
   young names). Stacking them only thins the book further (69→39 by, 55→24 bn) and *lowers*
   MAR vs rmom-alone. Per §7: **"age & rmom overlap (same factor); deploy the single better
   gate, not the stack."**

2. **ff6 ADDS → `NO`.** `age_rmom_ff6` pooled MAR Δ (+2.77) = `age_rmom` (+2.77), not greater,
   and ff6 produces 0 `exit_failed_fade` exits on either venue. ff6 is inert on an age+rmom book.

3. **Tier-2 eligibility:** `age_rmom`/`age_rmom_ff6` technically clear the Tier-2 demo-candidate
   bar (positive both venues, pooled MAR Δ +2.77, neither venue < −0.5, ≥30 by/≥20 bn) — but the
   stacking verdict says the stack is **not the thing to deploy**.

### Bottom line (honest, in-sample, Tier-2 ceiling)

- **The stack is not justified.** age + rmom **overlap**; adding rmom to age (or age to rmom)
  does not stack — it harvests the same squeeze factor and shrinks the book. **ff6 adds nothing**
  on top (0 catches).
- **`rmom`-alone has the highest MAR but is not obviously the better *deployment*:** its MAR is
  inflated by a near-zero DD on a ~60-trade/3yr book, it is recent-tilted (early ≈ flat), its
  bybit return-Δ p5 is ~0, and (per STATE) the rmom signal must be **live-wired** before any
  faithful forward demo. The fresh-median gate is also far more aggressive than P3b validated.
- **`age` remains the robust, deploy-ready single refinement** (E2): ample trades (579/307),
  bybit all-thirds-positive, binance recent rescued from −39% to ≈flat, simple PIT feature.
- **Recommendation:** do NOT deploy the combined stack. Keep **age** as the robust validated
  gate; treat **rmom-alone** as the higher-MAR-but-fragile lead that needs the engine-grade
  build + live-wiring + a funding-complete binance root before it can be trusted over age.
  This run **earns Tier-2 at most** (in-sample); the forward demo is the only Tier-3 arbiter.
