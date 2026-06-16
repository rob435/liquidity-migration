# Pre-registration: W6 — crowding-ADMISSION engine sweep

**Date:** 2026-06-15
**Author:** rob435 (operator-directed W6 bybit-first program)
**Stage:** run-pending (binding). Filed BEFORE the engine sweep, per AGENTS.md (touches the
per-venue full-PIT roots).

## Why this stage exists
A1 (sizing), A4 (hedge-regime), A5 (gross-scaler) all NULL/weak: the orderflow squeeze signal
does not harvest by selecting/sizing/timing the EXISTING book. The crowding-ADMISSION SCREEN
(`scripts/w6_crowding_admission_screen.py`, exploratory) found the program's first non-NULL
lead: the 1166 bybit fades the crowding gate REJECTS (`entry_crowding_max_fresh=2` skips the
3rd+ fresh candidate per signal-hour, with concurrency slots free) are **profitable — mean
+37bp/fade net of 15bp cost** (fade model validated vs the ledger, return corr 0.996, entry-px
MAPE 0.0). The diffuse-edge logic: the book profits when broadly deployed, so ADMITTING these
profitable rejected fades should ADD book profit. This is the one diffuse-edge-FRIENDLY
entry-set direction (it ADDS deployment rather than selecting/shrinking/sizing).

## What's changing (the intervention)
Sweep the EXISTING engine knob `entry_crowding_max_fresh` (per-component, frozen control = 2)
upward to admit more fresh fades per signal-hour: k ∈ {2 (control), 3, 4, 6, 999 (off)}. No
engine-code change — only `ContinuousEventConfig` overrides through `_component_config`. Run all
4 frozen components per k, rebuild the ensemble-hedged ledger, compute MAR.

Why MAR is the clean metric: MAR (= total_return / max_drawdown) is **leverage-invariant** —
uniform gross scaling leaves it unchanged. Admission adds DIFFERENT trades (breadth), not a
uniform scale, so a MAR change reflects the added fades' return/drawdown QUALITY
(diversification), not leverage. Confirmed by an explicit **constant-leverage control**: the
k=2 book scaled by admission's realized gross ratio via `size_mult_lookup = g` (uniform) — its
MAR must stay ≈ the k=2 control, so any admission MAR gain is breadth, not leverage.

## Arms (per venue)
- `K2` control (entry_crowding_max_fresh=2) = the frozen ensemble (MAR bybit ≈ 4.748).
- `K3, K4, K6, Koff` (=3/4/6/999).
- `LEV_g` constant-leverage control: K2 trades × uniform size_mult = the K4 realized gross ratio.
- Cost stress: the best admission cell + K2 at 1.5× round-trip cost.

## Decision rule (a priori) — bybit-primary
Crowding admission is a demo/paper FORWARD-WATCH candidate iff:
1. **bybit MAR(admission) > K2** for the admission cells **robustly** (not one cell — ≥2 of
   {K3,K4,K6} positive, and the trend not driven by a single k), AND
2. it **takes materially MORE trades** than K2 (genuine breadth add, not degenerate), AND
3. bybit MAR(best admission) **> the constant-leverage control** (the gain is breadth, not
   leverage), AND
4. **≥2/3 chronological thirds** bybit MAR(admission) ≥ K2, AND
5. survives **1.5× round-trip cost** (bybit MAR still > K2), AND
6. binance **not worse** (MAR(admission) ≥ K2 − ~0.5, i.e. not a venue artifact).
Report realized gross ratio + maxDD per cell (capacity/Tier-3 note — admission raises gross;
real deployment caps it; this is a research MAR finding, NOT a real-money claim). Tier-3
UNCHANGED.

Honest prior: GENUINE (the rejected fades are profitable and admission ADDS deployment — the
mode W5/W6 found harvests). Risks: (a) admitted fades may pile onto the same high-deployment
days → correlated squeeze-tail DD (mitigated by the live BTC-vol hedge, not in this ledger);
(b) `max_active=25` may re-cap admission on busy days, blunting the add; (c) the gain may be
leverage not breadth (the constant-leverage control decides). binance squeeze is data-gated, so
binance admission is blanket (no squeeze conditioning).

## Roots touched
- [ ] bybit_full_pit / binance_full_pit — engine reads (klines/funding/OI), writes reports under
  `reports/<run_tag>/`. No dataset-layer writes. Corrected funding guard applied (research-scoped).
- [x] forward demo/paper — only if it passes and the operator green-lights.

## Run command
```bash
POLARS_MAX_THREADS=8 PYTHONIOENCODING=utf-8 PYTHONPATH=. .venv/bin/python \
    scripts/w6_crowding_admission_sweep.py --venues bybit,binance \
    --out ~/SHARED_DATA/w6_crowding_admission_sweep_2026-06-15
```

## Post-run results

**Run 2026-06-15** (bybit quick-gate, `scripts/w6_crowding_admission_sweep.py`, K2/K4/K999 +
constant-leverage control + 1.5× cost). K2 reproduces the frozen ensemble (MAR 4.748).

| arm | trades | MAR | ΔMAR vs K2 | return | maxDD | notional |
|---|---:|---:|---:|---:|---:|---:|
| K2 (control) | 3220 | 4.748 | — | 0.7707 | −0.0527 | 44.09 |
| K4 | 3481 | 4.607 | **−0.141** | 0.7867 | −0.0554 | 47.39 |
| K999 (off) | 3505 | 4.673 | **−0.075** | 0.7959 | −0.0553 | 47.67 |
| LEV_g (K2 × 1.081) | 3220 | 4.916 | +0.168 | — | — | 47.67 |
| K2 @1.5× cost | 3220 | 4.119 | — | — | — | — |
| K999 @1.5× cost | 3505 | 4.033 | −0.086 | — | — | — |

Gate 1 FAILS: bybit ΔMAR < 0 for both admission cells. Admission DOES raise return
(0.77→0.80) and trades (+8%) — the rejected fades are profitable adds, as the screen showed —
but maxDD grows faster (−0.0527→−0.0554), so MAR falls. At MATCHED gross, admission (K999
4.673) is far worse than uniformly leveraging the existing book (LEV_g 4.916, +0.168) →
gate 3 (beats constant-leverage) FAILS decisively: the admitted fades are lower-quality,
tail-correlated marginal exposure. Cost stress also worse (K999 @1.5× ΔMAR −0.086). Per
Stage-4d discipline, the full grid + binance were NOT run (the cheap decisive gate failed on
the primary venue). (The LEV_g +0.168 is a hedge-ratio/leverage artifact — book larger vs a
fixed hedge — NOT a lead: it trades away the squeeze tail-protection and Tier-3 safety.)

## Verdict
**REJECTED (NULL).** The crowding gate (`entry_crowding_max_fresh=2`) is REJECTING fades that
are profitable in isolation (+37bp/fade, screen), but admitting them LOWERS MAR (−0.075 to
−0.14) because the marginal fades pile onto high-deployment days (concurrent correlated
squeezes) and concentrate the tail faster than they add return — worse even than a
gross-matched uniform leverage of the existing book. **The crowding cap is near-optimal; it
correctly controls the correlated squeeze tail.** This is the sharpened W5/W6 root cause: the
book's edge is diffuse but its TAIL is correlated-concurrent, so EVERY lever that adds/sizes
book exposure (A1 sizing, A5 gross, this admission) concentrates the tail → MAR falls; only
the BTC-vol HEDGE (tail protection without added exposure) harvests. **No deployment; Tier-3
unchanged.** With A1/A4/A5 + this, the orderflow squeeze axis (Track A) is CLOSED for
harvestable modes; the cost-alpha axis (Track B) is data-gated locally (no sub-hourly price).
