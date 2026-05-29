# Research summary — liquidity-migration strategy

**Updated 2026-05-29.** This is the **single consolidated research record**. It replaces
the Round 1 + Round 2 pre-registration plans and per-phase verdicts (deleted 2026-05-29;
full originals recoverable from git history). Methodology standard:
[backtesting_errors_we_never_repeat.md](backtesting_errors_we_never_repeat.md).

## What the strategy is

Cross-sectional **short** of mid-liquidity perp names (liquidity-rank **31–400**, the
top-30 most-liquid are excluded) that just had a **liquidity-migration event** — turnover
spike ≥6×, ≥150-place rank climb, a *modest* residual return ≥8%, strong close, top-10% of
events excluded. It fades the in-migrating price-insensitive flow; it does **not** time the
top or short the most extreme names. Daily signal cadence + 1h entry delay (Architecture A,
the variant running on the Bybit demo). A continuous/rolling variant (Architecture B) was
evaluated but never built into a backtest engine.

## Headline (the 2026-05-29 correction)

The earlier **"Round 2 = documented null"** verdict was substantially a **methodology
artifact**, not a property of the strategy. It rested on three pessimistic settings
stacked together: (1) **`bar_extreme`** stop fills (assume every stop covers at the bar's
worst intrabar wick), (2) **over-concentration** (`max_active=3`, the deployed demo value),
and (3) a **conservative ×3 = 45 bps** cost. Re-run under a **realistic capped stop fill**
(`bar_extreme_capped` 10%) at **sane concentration** (`max_active=12`), the daily strategy
is **positive on both venues in-sample** — it is **not** a null.

It remains **in-sample**; the forward demo (since 2026-05-22) is the arbiter, and nothing
is promoted to real money.

## Daily strategy — realistic re-baseline (full-PIT, in-sample 2023-04→2026-05)

| config | venue | stop fill | cost | total ret | max DD | worst day | Sharpe |
|---|---|---|---:|---:|---:|---:|---:|
| baseline, max_active=3 (DEPLOYED) | bybit | `bar_extreme` | 45bps | −32% | −87% | −36% | 0.19 |
| baseline, **max_active=12** | bybit | **capped 10%** | 45bps | **+37.8%** | −27.5% | −4.8% | **0.70** |
| baseline, **max_active=12** | binance | **capped 10%** | 45bps | −4.7% (**gross +16.1%**) | −33.6% | −4.4% | −0.05 |

The top row is the old worst-case (what the null was built on). At the **honest 15 bps**
cost (R6) the cost drag (bybit −28.7%, binance −17.9%) roughly **thirds** → bybit higher,
**binance ~breakeven-to-positive**. **Both venues are gross-positive.** (Binance funding
was not applied in this run; for a short, funding is typically a credit, so if anything
this understates binance.)

## Key findings worth keeping

1. **Concentration is the deployed config's main risk.** `max_active` 3→12 cuts the worst
   single day from **−36% → −4.8%** and max-DD from **−87% → −27.5%**. The demo runs 3; the
   research-validated value is 12. **Move the demo to 12** (or to risk-equal sizing).
2. **The stop-fill assumption dominated the old verdict.** `bar_extreme` (worst-case wick)
   vs a 10% cap swung the deployed curve from −32% to +479% (concentration-amplified). The
   engine default is now **`bar_extreme_capped` 10%** — realistic bad-case, not worst-case;
   calibratable from live-demo stop fills.
3. **Best profile found:** `drop_all_4` filters (drop day_return + stop_pressure +
   realized_loss + rank_max) + **`risk_equal` 2%** sizing + **`ff6_4pct`** failed-fade exit.
   `risk_equal` de-concentrates risk (cuts DD hard); `ff6_4pct` is the best loss-cutter;
   `drop_all_4` is the best filter set. Under `bar_extreme` this was bybit MAR 1.39; under
   the capped fill it would be higher (not re-run).
4. **Continuous signal (Architecture B) — real IC, not a short.** Rolling features carry a
   **genuine, cross-venue, robust** negative IC (composite −0.084/−0.085/−0.087 bybit &
   −0.078/−0.081/−0.085 binance at 24/72/168h; `rv_168h` −0.13 @168h, strengthening). This
   is **not** a fill artifact (IC/decile test, flat cost). BUT as a **short** it is not
   tradeable: the L/S decile is cost-dominated and the **extreme top decile (strongest
   short) RALLIES +26/+39 bps @168h**. The tradeable edge is the *opposite* direction — a
   **momentum (long the high-vol/extended names) thesis**, consistent cross-venue. **This is
   the strongest fresh-research lead.**
5. **Pre-2023 is structurally untradeable** (bybit had 7–182 symbols; rank-31–400 with a
   ≥150-place climb needs the 400+ universe that only existed from ~mid-2024). There is **no
   internal OOS root** — pristine OOS is the forward demo (see [data_roots.md](data_roots.md)).

## Methodology lessons (kept from the audit hardening)

The engine was hardened (2026-05-29) toward honesty and that direction was correct:
`stop`→`bar_extreme` stops (was optimistic trigger fills — error #14), 100% taker (was a
0.6-maker blend — #6), calendar-exact returns (#13), real promotion gates, full-PIT
survivorship (#1). **The over-correction** was making the worst-case `bar_extreme` the
*default* — too brutal on illiquid 1h alts (real stop slip is a median +2.3%, but it
assumed wick-tops up to +89%). Fixed: `bar_extreme_capped` 10% default. Validation design
= cross-venue agreement + forward demo (no internal OOS).

## Was "Round 2 = null" right?

**No — for the daily architecture.** The daily documented-null was a worst-case-methodology
artifact; realistically both venues are positive in-sample. **The continuous-architecture
null is real** (robust, not fill-dependent) but it surfaced the momentum-continuation lead.
Net: the strategy is **not dead**. Next steps: (a) move the deployed config to
`max_active=12` + capped fills, (b) let the forward demo confirm, (c) a **fresh
pre-registration** for the momentum-continuation thesis (the opposite-sign edge the
continuous decile profile revealed), (d) a clean re-baseline of the best stack at the
capped fill + honest 15 bps.

## Provenance

Round 1 + Round 2 pre-registration plans and per-phase verdicts (phase0–6, R1–R13, C0–C3)
were consolidated here and **deleted 2026-05-29**; full originals are in git history.
Backtest report artifacts live under the data roots (see [data_roots.md](data_roots.md)).
Engine/methodology change receipts are in the git commit log.
