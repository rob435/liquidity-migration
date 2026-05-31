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
