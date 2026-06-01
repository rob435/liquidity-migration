# Pre-registration — P1c/Avenue D (FINAL): continuous is a real all-weather SIGNAL but a weak tradeable BOOK

**Date:** 2026-06-01 · **Stage:** EXPLORATORY (characterization + reasoned cost/capacity; NOT promotion evidence)
**Plan:** `docs/research_plan_continuous_fade.md` · **Closes** the continuous-fade program.
**Builds on:** `p0-continuous-rmom-2026-05-31.md`, `p1b-continuous-intraday-fade-2026-06-01.md`.

## The full arc (honest, including a retraction)

1. **Phase 0 (PASS, signal):** the PIT-clean **rmom squeeze-filter (NOT age)** flips the continuous rolling
   short **all-weather** both venues (where c2b's age-only flip was recent-only). BB1+BB2 beatable at signal.
2. **Avenue D first pass (24h hold) → WRONG NULL ("daily cadence load-bearing"):** a **hold-span artifact**
   (a 24h hold from a late entry-hour spans the next day's pump). Retracted (`p1-continuous-daily-cycle`).
3. **Avenue D corrected (p1f/p1g):** the **per-hour fade RATE is positive at EVERY hour** (FRESH-entry 6h/h:
   close +12.9/+12.1, midday +15.4/+15.0, late +8.2/+6.5 bps/h bybit/binance, all EARLY-positive). The fade
   is a **real all-weather all-day intraday process** → continuous viable *at the signal level*.
4. **Portfolio proxy (p1h) → INVALID.** Booking the per-trade edge with equal weight and compounding daily
   over ~1150 days at ~12× daily turnover produced **absurd MARs (24,000+) / Sharpe 10** — it compounds a
   high-frequency *look-ahead* edge with **no capacity or fill realism** (assumes unlimited 15-bps mid fills).
   Only its *structure* is informative (DD 4-8%, no catastrophic day, positive both eras → diversified).
   **Run label: `invalid` — do not cite its return/MAR.**
5. **Capacity + cost reality (this receipt) → the binding constraint.**

## The capacity + cost finding (the decisive practical gate)

**Capacity is small and shrinking** (D9 = mid-liquidity alts; `turnover_quote`, fresh-entry hourly USD):

| | bybit | binance |
|---|--:|--:|
| median hourly turnover/name | $101k (EARLY $155k → **RECENT $52k**) | $518k (EARLY $946k → RECENT $222k) |
| % entries in <$50k/h names | **35.5%** | 7.1% |
| est. book @1% / @5% participation | **$0.10M / $0.51M** | $0.53M / $2.6M |

**The real reason the daily cadence wins (NOT the retracted daily-cycle artifact — a sound COST argument):**
the continuous **6h-hold** gross edge (~+85 bps) is the same order as a realistic round-trip cost on these
names (spread + 15-bps taker ×2 + impact ≈ 100+ bps at any real size), so it is **impact-fragile**; the
daily **24h-3d hold** captures a ~3-4× larger per-trade move that comfortably clears the same cost:

| hold | gross fade (bps) | net @45 | net @100 | net @150 |
|---|--:|--:|--:|--:|
| **6h continuous** | ~+85 | +40 | **−15** | **−65** |
| 24h close (= the daily entry) | ~+319 | +274 | +219 | +169 |
| 24h off-close (continuous breadth) | ~+130 | +85 | +30 | −20 |

The **15-bps mid-fill** assumption used in every EXPLORATORY char (and that made p1h explode) is the
optimistic illusion; at honest illiquid-alt costs the short-hold continuous edge collapses. Continuous also
runs **~12× the daily's turnover** on the **same illiquid names** → strictly worse capacity than the daily.

## FINAL VERDICT — continuous is a real all-weather SIGNAL, but NOT a worthwhile tradeable book; daily wins

- **The signal is real and all-weather** (the retraction stands — the fade is an all-day intraday process,
  not daily-cycle-locked; both boss battles fall to the rmom gate). The mission's thesis ("you *can* short
  the state at any hour") is **vindicated at the signal level.**
- **But the tradeable case is weak**, for sound economic reasons (not the retracted artifact): (a) **small,
  shrinking capacity** (~$0.1-0.5M bybit, the funding-real anchor; 35% of entries near-untradeable); (b) the
  **short-hold edge is impact-fragile** on illiquid alts at ~12× daily turnover; (c) the **long-hold
  continuous** only adds **lower-edge off-close breadth** (the daily already captures the highest-edge entry
  — at the close — cost-robustly). Funding ≈0 (P0c) is the one tailwind, but it doesn't rescue the cost case.
- **Recommendation: do NOT build the continuous engine.** The program's robust, cost-survivable edge remains
  the **daily age+rmom strategy** — now better understood: its longer hold is *load-bearing for cost
  robustness* on the illiquid mid-cap alt space, which the short continuous hold cannot match.

## What would change this (the operator-gated path, if pursued)

A continuous engine backtest with a **realistic impact/capacity model** (cost = spread + taker + k·(size/ADV),
not 15-bps mid) and **additive (non-compounding) returns**, at a deployable size, both venues, early/recent.
The cost economics above predict the short-hold version disappoints; a long-hold (24h) continuous overlay
that only adds off-close breadth at small size is the most it could be — marginal vs the daily. Given the
capacity ceiling, this is low-priority; **flagged for the operator, not pursued autonomously.**

## Byproducts kept

(1) **rmom reconfirmed** as the all-weather cross-sectional fade gate (independent of the daily event filter).
(2) **binance funding is present (99.8% cov) and ≈0**, not "funding-blind" (corrects §1 + memory).
(3) The deployed daily strategy's design (late entry, multi-day hold) is **validated as cost-robust**, not
just a clock-proxy. (4) **Lesson logged:** never finalize a null on a single hold horizon; and never trust a
proxy MAR that assumes mid-fills on illiquid names — model impact, or the edge is an illusion.

Artifacts: `~/SHARED_DATA/p1{f,g,h}_*_2026-06-01.{out,json}`, capacity probe output (this session);
scripts `scripts/p1{d,f,g,h}_continuous_*.py`. Label: **EXPLORATORY** (p1h: **invalid**). Never promotion evidence.
