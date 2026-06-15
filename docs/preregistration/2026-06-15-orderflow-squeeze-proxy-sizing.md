# Pre-registration: orderflow squeeze-proxy sizing tilt (engine intervention)

**Date:** 2026-06-15
**Author:** rob435 (operator-directed orderflow program, 2026-06-15)
**Stage:** run-pending (binding). Filed BEFORE the engine sweep, per AGENTS.md
parameter pre-registration (touches the per-venue full-PIT working roots).

## Why this stage exists
W5 closed the price/return lever space but left the OI/funding/depth squeeze proxy
as the credible untested orderflow leg. The EXPLORATORY screen
(`scripts/orderflow_squeeze_proxy_screen.py`, artifacts
`~/SHARED_DATA/orderflow_squeeze_proxy_screen_2026-06-15/`, run_label exploratory)
found, within-symbol partial rank-IC over the production composite (symbol-hash
control DEGENERATE on both venues, as required):

- **`oi_chg_24h`** (24h OI buildup before entry): **bybit IC +0.0665, perm p=0.002,
  n=2153, thirds +0.041/+0.083/+0.083 (all positive).** Binance not evaluable (OI
  history ~6 weeks → 0 coverage; data-gated, see the liquidation/depth + P11 tracks).
- **`funding_level`** at entry: **binance IC +0.0564, p=0.013, n=2924, thirds all
  positive; bybit IC +0.0246 (p=0.22, same sign).** The only same-sign-both-venue leg.
- oi_chg_6h, premium_level, premium_chg_24h: not leads (NS and/or data-thin).

Sign matches the thesis: a pump on a CROWDED long (OI buildup / high funding) is a
squeeze that fades harder, so the book's SHORT does better. This is admissibility,
NOT a harvest: the IC magnitude (~0.066) is the same order as the Stage 7b path-shape
IC that Stage 5 sizing FAILED to harvest. This stage tests whether it harvests.

## What's changing (the intervention)
A causal, **mean-1, gross-neutral squeeze-proxy SIZING tilt** on the continuous fade
book — size UP entries with a high squeeze-proxy and DOWN entries with a low one,
keeping breadth and gross unchanged (same trades; only per-name notional moves). This
is the Stage-5 sizing harness, re-pointed at the squeeze proxy instead of path-shape:

- Squeeze score per entry, causal at the signal bar:
  `sq = z(oi_chg_24h)` (bybit primary), with a both-venue variant `sq = z(oi_chg_24h)
  + z(funding_level)` where OI exists, and a **funding-only** variant `sq =
  z(funding_level)` for binance (OI-gated). z = within-symbol standardization
  (the screen statistic's space), clipped to [-3, 3].
- Sizing multiplier `m = clip(1 + k·sq, 1-cap, 1+cap)`, then renormalized per
  rebalance day so the booked gross is unchanged (mean-1). Grid: `k ∈ {0.25, 0.5}`,
  `cap ∈ {0.5, 1.0}`.
- Applied through the existing Stage-5 `size_mult_lookup` hook
  (`continuous_events._run_trades`), so entries/breadth/exits are byte-identical and
  only per-name notional + resize/impact cost change. `size_mult_lookup=None` →
  byte-identical control (already tested).

## Exact knobs / files
- `scripts/` new stage runner (Stage-5 clone) computing the squeeze score from the
  full-PIT OI/funding layers and feeding `size_mult_lookup`. No engine code change
  beyond the already-merged hook.
- Roots read-only; no selection/threshold/universe knob is tuned on the roots.

## Hypothesis & predicted direction
The squeeze proxy carries a real within-symbol selection IC, so sizing UP the
high-squeeze fades should lift pooled MAR IF the edge is harvestable. PRIOR is
guarded: W5's root cause is that the book profits when broadly deployed, so sizing
interventions have repeatedly failed to harvest a real IC (Stage 5 path-shape, Stage
9 vol-sizing). Predicted most-likely outcome: **bybit-positive (OI leg), binance
weak/funding-only; harvest uncertain.** A symbol-identity / random-tilt control is the
decisive falsifier (Stage 5 lesson: cross-symbol "edge" was symbol-identity luck).

## Decision rule (a priori) — operator bar 2026-06-15 (bybit-primary, both-venue-aware)
A squeeze-proxy sizing tilt is a demo/paper FORWARD-WATCH candidate iff:
1. **bybit ROBUST:** pooled-bybit ΔMAR > 0 across the WHOLE `k×cap` grid (not one
   cell), AND
2. beats a **within-symbol-shuffled squeeze control** AND a **random per-symbol tilt
   control** (multi-seed, ≥5 seeds — the Stage-4d lesson: single-seed nulls have huge
   MAR variance), AND
3. ≥2/3 chronological thirds same-sign on bybit, AND
4. survives the realistic→1.5× resize/impact cost stress, AND
5. binance is "not completely losing" (ΔMAR ≥ ~0 on the funding-only variant; OI leg
   is forward-gated, evaluated later via the binance OI tape / P11).
Otherwise: the signal is **admissible but not harvestable** (logged as a forward-watch
note like the Stage 7b path-shape IC), and no sizing tilt is deployed.

Tier interaction: this does not clear any Tier by itself. **Tier-3 real-money gate
UNCHANGED.** A pass is a forward-watch adoption case, evaluated on forward demo/paper,
operator-gated. Do NOT set `REAL_MONEY=true`.

## Roots that will be touched
- [ ] bybit_full_pit — read-only; OI/funding layers + the four frozen component
  ledgers. No candidate selection.
- [ ] binance_full_pit — read-only; funding layer (OI gated).
- [x] forward demo/paper — only if it passes and the operator green-lights a tilt.

## Run command (to fill at run time, on the data box)
```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
    scripts/orderflow_squeeze_proxy_sizing.py \
    --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
    --grid k=0.25,0.5 cap=0.5,1.0 --seeds 0..4 \
    --out ~/SHARED_DATA/orderflow_squeeze_proxy_sizing_2026-06-15
# gate: ruff + pytest before any push.
```

## Post-run results
(fill after the sweep: per-grid-cell pooled/per-venue ΔMAR, control deltas, thirds,
cost-stress, verdict.)

## Verdict
accepted | rejected | inconclusive — pending the engine sweep.
Honest prior: a real IC that may or may not harvest; the controls + cost stress decide.
