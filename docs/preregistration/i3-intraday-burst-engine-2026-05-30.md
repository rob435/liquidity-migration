# Pre-registration — I3: engine-grade validation of the intraday extreme-burst short

**Date:** 2026-05-30 · **Gated on:** I2 (`i2-intraday-burst-selector-2026-05-30.md`) — a real,
promising, cross-venue, all-weather lead with a wide stop, found on a **Stage-B proxy**.
**Stage:** CANDIDATE (Tier-2 + Tier-3 residual). **Operator-gated** (expensive build). **5950X full-PIT.**

## Why I3 (what I2 left open)

I2 showed the extreme-pump-burst short, with a **wide (30–50%) stop**, is net-positive both venues ×
both eras × both costs (MAR ~3–8, DD 9–16% net45) — but on a **portfolio PROXY** (P&L booked at
entry-day; approximate concurrency; flat 2% stop-slip; **no funding**). I3 must confirm the lead under
a faithful engine before any deployment claim. The open risks I3 settles:
1. **Proxy → engine drift:** real exit-timing (a stopped position frees its `max_active` slot when the
   stop actually hits, not at entry-day) + true concurrency can change MAR/DD.
2. **Wide-stop fill realism (the key risk):** a 30–50% stop can **gap far worse in a squeeze** than the
   flat −(stop+2%) the proxy assumed. Use `bar_extreme_capped` (the engine default) — the realistic
   bad-case — and report stop-fill sensitivity (`stop` vs `bar_extreme_capped` vs `bar_extreme`).
3. **Funding (unmodeled, sign unknown):** holding a short through a pump-long-crowding window —
   funding may be a **credit** (shorts receive when funding is positive) OR a drag. Model per-venue
   funding over the hold (bybit funding present; binance funding present in raw `binance_usdm_funding`).
4. **STR-factor uniqueness:** is this the known short-term-reversal factor, or unique alpha?

## Method (selector FROZEN from I1b/I2; no tuning on the result)

- Selector: age≥300, liq-rank 31–400; burst gain≥8% + hourly vol-spike≥5×; EXTREME = top-tercile
  z(idio)+z(vel3)+z(vol_spike); first/day; cooldown 3d; short at burst+1h.
- **Engine-grade execution:** true event-driven portfolio (entry/exit ordering, `max_active=12`,
  risk_equal 2%, cooldown), `bar_extreme_capped` stop fills, **funding applied over the hold**, 15 &
  45 bps. Exit: stop (sweep width 20/30/50% **transparently — report all, don't pick one on IS**) OR
  48h max-hold.
- Map to the existing machinery where possible: reuse `risk_model.decompose_strategy_pnl` for the
  residual; consider whether the `volume-events` engine can host an intraday-burst entry path (else a
  faithful standalone event-driven backtester, validated against the I2 proxy within tolerance).

## Decision (pre-committed)

- **Tier-2 (r1_robustness):** return positive BOTH venues; **all-thirds-positive both venues** (c2b
  guard — the proxy was back-loaded, so this is the bar that matters); pooled MAR Δ vs all-bursts > +0.1;
  ≥30/20 trades; bootstrap p5 / LOO reported.
- **Tier-3 residual:** `risk_model` residual Sharpe ≥ +0.3 cross-venue AND survives adding a
  short-term-reversal factor to the model (else it's the known STR factor — tradeable if net-positive,
  but label honestly, not "unique alpha").
- **Funding sanity:** report the funding contribution; if it flips the sign, that's the headline.

## Falsifiers (STOP / honest null)

- Engine (capped fills + funding + true concurrency) turns it net-negative or recent-only; OR the
  wide-stop realistic fills produce a brutal DD; OR proxy→engine drift is large and adverse; OR no
  residual beyond the STR factor AND not net-tradeable after funding.

## Status

PENDING — **operator-gated** (this is the ~multi-day engine build; surface the I2 lead + this plan to
the operator for a go/no-go first). I4 (live WS + forward demo) gated on I3 + explicit operator go.
Demo only; never real money; commit never push.
