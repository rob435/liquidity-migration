# Pre-registration: Binance funding dataset rebuild (coverage 51 → full PIT universe)

**Date:** 2026-06-09
**Author:** claude (for owner)
**Stage:** run-complete — rebuilt basis accepted

## What's changing

The `binance_full_pit/binance_usdm_funding` working dataset is rebuilt from the
survivorship-free data.binance.vision monthly `fundingRate` archives (+ fapi REST
top-up for the current partial month) over the FULL PIT symbol set (~697 symbols from
kline coverage, delisted included — verified the archive serves delisted names, e.g.
DOTECOUSDT 2021). Current state: only 51 symbols have funding rows, so 296/307
baseline short trades book zero funding (2026-06-09 audit). The vision CSVs carry the
TRUE `funding_interval_hours` per settlement, so the rebuild also fixes the
`funding_interval_min=480` hardcode for Binance (the audit's second recommendation).
The old dataset directory is preserved as a sibling backup for instant rollback.

## Hypothesis

This is a data-correctness fix, not alpha: this strategy's shorts PAY funding on net
(Bybit ledger: −11.6% summed over 596 trades), so completing Binance funding coverage
should move Binance short results DOWN toward honesty.

## Predicted direction + magnitude

- Binance baseline (ungated) total return: **−3% to −6% absolute** on the 3-year
  window (the proxy band from the gap decomposition: −5.8%, IQR −3.1%..+1.5%).
- Bybit: unchanged (its funding was already complete).
- Trade selection: UNCHANGED (funding is charged at P&L, not used by any active gate).

## Roots that will be touched

- [x] binance_full_pit — `binance_usdm_funding` REPLACED (old dir kept as backup).
- [ ] bybit_full_pit — untouched.
- [ ] forward demo/paper — untouched (live funding comes from the venue, not this root).

## Decision rule (a priori)

This is not an accept/reject candidate — the rebuild lands regardless (correctness).
The bound follow-up: **re-run the two Binance Tier-2 gate cells** (00_baseline,
01_uptrend) on the rebuilt root and re-apply r1_robustness. If the re-measured gate
verdict drops below DEMO-ELIGIBLE (pooled MAR Δ ≤ +0.1 or binance MAR Δ < −0.5 or a
return sign flip), the gate receipt and STATE.md MUST be downgraded accordingly — the
honest number wins, no rescue.

## Run command

```bash
POLARS_MAX_THREADS=4 .venv/bin/python scripts/backfill_binance_funding_vision.py
# then re-measure:
.venv/bin/python scripts/btc_trend_gate_run.py --gate off     --root ~/SHARED_DATA/binance_full_pit --start 2023-04-01 --end 2026-05-28 --out ~/SHARED_DATA/binance_full_pit/reports/btc_gate_tier2_2026-06-09_refunded/00_baseline
.venv/bin/python scripts/btc_trend_gate_run.py --gate uptrend --root ~/SHARED_DATA/binance_full_pit --start 2023-04-01 --end 2026-05-28 --out ~/SHARED_DATA/binance_full_pit/reports/btc_gate_tier2_2026-06-09_refunded/01_uptrend
```

## Post-run results

Rebuild: **697 symbols / ~2.23M settlement rows** (was 51 / 129k), true per-settlement
intervals stored (60min: 72k rows, 120min: 3.5k, 240min: 1.18M, 480min: 967k; modal
per symbol: 454 @4h, 234 @8h, 9 @1h). Old dataset preserved at
`binance_usdm_funding.pre_rebuild_2026-06-09.bak`. Vision zips cached under
`_funding_rebuild_cache` (idempotent re-runs).

Re-measured Binance gate cells (`btc_gate_tier2_2026-06-09_refunded/`, both
full-PIT clean, trades unchanged 307/181 — funding is P&L-only as predicted):

| cell | pre-rebuild | refunded | Δ |
|---|---|---|---|
| 00_baseline | +18.7% / −17.0% DD | **+14.2% / −19.5%** | −4.5% abs (predicted −3..−6 ✓) |
| 01_uptrend | +8.1% / −11.8% | **+6.9% / −12.6%** | −1.2% abs |

Gate verdict re-check (engine MAR, 3.083y window): binance 0.226→0.174 gated,
MAR Δ **−0.05** (pre-rebuild −0.12); pooled with bybit +1.52 → **+0.73** > +0.1;
return positive; trades clear. **DEMO-ELIGIBLE HOLDS — marginally stronger pooled.**
Residual FUNDING_PARTIAL warning remains (some archive months 404; far smaller hole).

## Verdict

**accepted (landed)** — the cross-venue arbiter now charges honest Binance funding
with true settlement intervals. All future Binance numbers move to this basis;
pre-rebuild Binance results should be discounted ~3-6% absolute on 3y windows.
