# Bybit upper_wick Entry Sizing — GLOBAL Construction (2026-06-20)

Author: Claude (operator goal: find a working configuration)
Run label: `exploratory`. **`REAL_MONEY` false. Not activated live.**

## ⚠️ VERDICT (2026-06-20, 7-seed null): FAVORABLE TAIL, NOT SIGNIFICANT — the single-seed "pass" was inflated

The headline below beat the hash null on ONE permutation seed by +1.13. That seed (seed0=6.117)
was a lucky-LOW shuffle. The full 7-seed permutation null tells the real story:

```
MAR null (7 seeds, sorted):  5.253  5.508  5.798  5.950  6.117  7.017  7.517   (mean 6.166, sd 0.76)
upper_wick (real):           7.250   -> beats 6 of 7,  permutation p = 0.25,  z = +1.4
total-return null (6):       0.2624 0.2706 0.2711 0.2780 0.2809 0.2824
upper_wick (real):           0.2843  -> beats 6 of 6,  permutation p = 0.14
```

So upper_wick sits at the **favorable tail** (~86th pct MAR, top on return) but does **NOT reach
significance** (p = 0.25 MAR / 0.14 return; need < 0.05). The single-seed +1.13 was an artifact
of comparing against one lucky-low shuffle. **Root cause of the wide null:** the 2f hedge makes
the drawdown denominator tiny (~1.3%), so MAR is hypersensitive to ANY within-book reweighting —
random permutations span 5.25–7.52. The real upper_wick IC (+0.146) is a genuine but small nudge
that the MAR noise mostly swamps. Leaning real, not confirmed — consistent with the program's
"real IC, not (yet) tradable" theme. NOT activated; flag stays False.

**Follow-up running:** a GATES-OFF larger-N power test (~1,560 decisions vs 932) to tighten the
null and resolve the borderline — `scripts/continuous_v2_gatesoff_upperwick.py`,
`backtest-runs/continuous_v2_gatesoff_2026-06-20/`. A gate-off pass is mechanism evidence on a
different book, NOT deployment evidence for the gated book. The text below is the original
single-seed write-up, kept for the audit trail.

---

## Context — why this exists

The per-symbol-expanding-z upper_wick sizing was RETRACTED as an artifact (duplicate-counting
+ sparse history → only 0.6% of trades tilted → inert; corrected = −0.003, below hash). But
the OOS proxy that first showed signal used a GLOBAL (pooled) standardization, which I never
full-ledger-confirmed. This tests the correct, ACTIVE construction.

## Construction (parity-clean)

`build_upperwick_lookup(..., mode="global", vol_attenuate=True)`: causal GLOBAL expanding-z of
the per-decision upper_wick (pooled across ALL symbols, prior-only), vol-attenuated, clipped
[0.5,1.5]. PRINCIPLED per-decision history (the 3 components share (symbol,signal_ts) ~61% →
counted once). Active: **98% of trades tilted** (vs per-symbol's 0.6%).

## Full-ledger result (Bybit, deduped per-decision, leverage-controlled by hash)

| arm | MAR | total | max_dd | early MAR | late MAR |
|-----|----:|------:|-------:|----------:|---------:|
| control | 6.387 | 0.2599 | −0.0130 | 4.075 | 26.225 |
| **upper_wick global-z** | **7.250** | 0.2843 | −0.0125 | 4.588 | 29.387 |
| hash (same mult dist) | 6.117 | 0.2624 | −0.0137 | 3.850 | 24.263 |

- **+0.863 MAR vs control, +1.133 vs hash, passes=True.**
- **OOS-stable:** beats control (+0.51 early / +3.16 late) AND hash (+0.74 / +5.12) in BOTH
  halves — not a single-period fluke.
- **Leverage-controlled:** the hash carries the same multiplier distribution (same ~1.04 mean);
  it lands BELOW control (random sizing adds concentration → worse), so the +1.13 over hash is
  real timing/selection, not the small mean drift.
- **Drawdown-neutral-to-better** (−1.30% → −1.25%).

## HONEST magnitude framing

The absolute effect is MODEST: upper_wick adds **+2.2pp total return vs hash over ~3 years**,
with a 0.05pp drawdown improvement. The MAR headline (6.39→7.25) is AMPLIFIED because the 2f
hedge makes the drawdown denominator tiny (~1.3%), so small return/drawdown gains lever into a
notable MAR. Read it as "a real, modest entry-sizing edge," not a transformation.

## Why this is believable where per-symbol was an artifact

- per-symbol failed because it was INERT (sparse history → no tilt) AND had a duplicate-counting
  bug. global-z has NO bug — it is a clean application of a real IC (upper_wick +0.146 on gross,
  IC_mae −0.005 = clean) to 98% of trades.
- OOS split (both halves), hash control (leverage + distribution), and drawdown-neutrality all
  support it. The live↔backtest parity machinery is bit-exact (feature 1e-16, mult 3e-16).

## Caveats / open items (NOT yet deployable)

- **Hash-seed robustness:** PENDING — 2 extra hash seeds running (the headline uses 1 seed).
- **Construction selection:** global was chosen after per-symbol failed; it is the mechanically
  correct ACTIVE construction (and independently the OOS-proxy construction), but disclose the
  two-construction comparison.
- **In-sample / Bybit-only / no forward evidence.** A modest in-sample edge is not promotion
  evidence; forward demo/paper is the arbiter.
- **The live sizer is PER-SYMBOL** (the failed construction). Deploying the global construction
  needs a GLOBAL live sizer (pooled history) + a fresh parity reconcile — a follow-up. The
  current override flag stays False.
- `lower_wick` (operator idea) is NOT an inverse signal — it is +0.139 (both wicks proxy
  wickiness); the asymmetry uw−lw is dead (+0.006). upper_wick is the clean one.

## No real-money / promotion claim

`REAL_MONEY` false. Not activated. Forward demo/paper remains the arbiter.
