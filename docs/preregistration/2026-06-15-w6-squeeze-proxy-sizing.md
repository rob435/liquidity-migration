# Pre-registration: W6 squeeze-proxy sizing tilt (engine intervention)

**Date:** 2026-06-15
**Author:** rob435 (operator-directed W6 bybit-first program, 2026-06-15)
**Stage:** run-pending (binding). Filed BEFORE the engine sweep, per AGENTS.md
parameter pre-registration (touches the per-venue full-PIT working roots).

## Why this stage exists
W5 closed the price/return lever space but left the OI/funding/depth squeeze proxy
as the credible untested orderflow leg. The EXPLORATORY screen
(`scripts/w6_squeeze_proxy_screen.py`, artifacts
`~/SHARED_DATA/w6_squeeze_proxy_screen_2026-06-15/`, run_label exploratory)
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
    scripts/w6_squeeze_proxy_sizing.py \
    --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
    --grid k=0.25,0.5 cap=0.5,1.0 --seeds 0..4 \
    --out ~/SHARED_DATA/w6_squeeze_proxy_sizing_2026-06-15
# gate: ruff + pytest before any push.
```

## Post-run results

**Run 2026-06-15** (`scripts/w6_squeeze_proxy_sizing.py`, harness validated: S0_control
reproduces the Stage-0 ensemble exactly — bybit MAR 4.748). Implementation notes / deviations
from the literal receipt, all documented and defensible:
- The within-symbol z standardizer is a **causal expanding** per-symbol mean + **causal
  expanding** residual sigma (not a static train-fold baseline). Reason: the train-fold
  baseline discarded ~70% of OI-covered entries (n_tilted 2182→680); the expanding form is
  fully causal and preserves coverage. (Methodology-review finding; the prior global-sigma
  draft was a whole-sample look-ahead feeding the cap-clip and was fixed.)
- Squeeze score keyed on **unique (symbol, signal_ts)** (the score is component-independent;
  the engine applies the multiplier shared across components). 3220 selected rows → 1326
  unique entries; OI covers 925/1326, n_tilted 646 after the first-per-symbol drop.
- **FUNDING-DATA GOTCHA (documented separately):** the engine's new
  `_assert_funding_one_per_settlement` guard false-positives on ~89 bybit symbols that
  genuinely settle sub-8h (verified row-for-row vs the authoritative `get_funding_rate_history`
  endpoint; `instruments.fundingInterval=240` for ANIMEUSDT). The funding CHARGE is correct
  (exact-stamp on raw `funding_rate`). The run installs a research-scoped corrected guard
  (fires only on <55min cadence). See `docs/audit/2026-06-15-funding-interval-mislabel-guard-falsepositive.md`.

**OI score (bybit primary), k×cap grid ΔMAR vs S0 (gross-neutral confirmed, ratios 0.998–1.003):**

| cell | MAR | ΔMAR | return | maxDD |
|---|---:|---:|---:|---:|
| S0_control | 4.748 | +0.000 | 0.7707 | −0.0527 |
| k0.25/cap0.5 | 4.729 | −0.019 | 0.7566 | −0.0519 |
| k0.25/cap1.0 | 4.756 | +0.008 | 0.7578 | −0.0517 |
| k0.5/cap0.5 | 4.350 | **−0.398** | 0.7830 | −0.0584 |
| k0.5/cap1.0 | 4.490 | **−0.257** | 0.8001 | −0.0578 |

**Gate 1 (bybit ΔMAR>0 across the WHOLE grid): FAIL** (3/4 cells negative). Returns rise
monotonically with tilt strength (0.757→0.800) — the OI squeeze IC is REAL — but maxDD grows
faster than return, so MAR falls. Gross-neutral (gate 6 pass), so this is a genuine
risk-adjusted failure, not leverage. Gates 2–5 not evaluated: per Stage-4d compute discipline,
the decisive cheap gate (1) already fails on the STRONGEST leg.

**Funding / combined sizing legs: NOT RUN (by discipline).** Bybit within-symbol funding IC is
insignificant (screen: +0.025, p=0.22; entry-funding screen also weak, p=0.111), and the
DD-concentration mechanism that killed OI sizing is generic to any mean-1 size-up on this
diffuse book. Spending ~2h of engine on a weaker, primary-venue-insignificant leg is exactly
the low-prior spend the discipline forbids. Binance funding-sizing (binance IC +0.056) is a
secondary-venue note, not a primary candidate.

## Verdict
**REJECTED (admissible, NOT harvestable).** The OI-buildup squeeze proxy carries a real
within-symbol selection IC (Stage-0 screen +0.0665, p=0.002; here return rises monotonically
with tilt), but sizing UP the high-squeeze fades does NOT lift risk-adjusted return — MAR
falls because drawdown concentrates faster than return grows, gross held at 1.0. This is the
**W5 root cause confirmed on the W6 orderflow axis**: the continuous fade book's edge is
diffuse and profits when broadly deployed, so every selection/sizing lever (entry priority,
path-shape, liquidity, now OI-squeeze) fails to harvest a real IC. Logged as a forward-watch
note; **no sizing tilt deployed; Tier-3 unchanged.** Next: A4 (squeeze × hedge-intensity
overlay) — the proven non-selection mode that keeps the whole book.
