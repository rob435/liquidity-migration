# Pre-registration — does the failed-fade exit (ff6) improve the age-alone book?

**Date:** 2026-05-31
**Run label:** VALIDATED (per `parameter_pre_registration.md` — MAY be cited as evidence in a forward-demo decision)
**Author/owner:** rob435 (operator-directed: "ff6 needs to be tested on age alone")
**Status:** PLANNED → RUNNING (forced onto the 16 GB research box via swap; not the 5950X)

## 0. Why this run exists (the gap left by `age-rmom-ff6-combined-2026-05-31`)

The combined run measured ff6 **only on top of `age_rmom`**, where it caught **0**
trades — but the mechanism it credited was **rmom** (rmom screens out
already-pumping names *before* entry, leaving ff6 no failed fades to cut *after*).
ff6 was **never measured on the age-alone book**. Since `age` is the recommended
deploy gate (and rmom is being shelved), the open and decision-relevant question is:
**does ff6 add on top of `age` by itself?** `age` is a far weaker anti-squeeze
filter than rmom (it cuts 27–41% of trades vs rmom's ~90%, leaving 579 by / 307 bn),
so unlike the rmom book it should still contain failed fades for ff6 to catch.

## 1. Hypothesis (one sentence, falsifiable)

On the same full-PIT daily event population with the **age300 gate**, adding the
**ff6_4pct failed-fade exit** raises MAR on both venues (R13 pattern: bybit DD-shave,
binance return-lift), and catches **> 0** trades — i.e. ff6 is NOT inert once rmom is
absent.

## 2. Decision rule (pre-committed — three-tier demo-arbiter, MAR-primary)

This run can earn at most **Tier-2 (demo-candidate)** — it is in-sample; the forward
demo is the only Tier-3 arbiter. Because ff6 changes **exit only**, `age` and
`age_ff6` have **identical entries**; the ff6 delta is a pure exit-rule effect
(R13 design #19). The control is `age` (re-run on this box in the same batch, so the
control and treatment share machine + engine version).

**ff6 ADDS** (the headline verdict, pre-committed):
- ff6 ADDS iff `age_ff6` pooled MAR Δ (mean of the two venue MAR deltas vs `age`) > 0
  AND ff6 does not turn either venue negative AND ff6 catches > 0 trades.
- **ff6 INERT** if `exit_failed_fade` count = 0 on both venues (byte-identical to `age`).
- **ff6 HURTS** if `age_ff6` pooled MAR Δ < 0 OR ff6 flips a venue's return sign.

Tier-2 demo-candidate framing for `age_ff6` (already cleared by `age`): positive return
both venues, ≥30 by / ≥20 bn trades, fragility REPORTED non-blocking.

## 3. Parameters under test (frozen before the run)

| cell-id | age (`pit-age-days-min`) | ff6 (failed-fade) | role |
|---|---|---|---|
| `age` | 300 | off | control (pure age book) |
| `age_ff6` | 300 | **on** (6h / 4% / 1% mfe / cloc 0.0) | treatment |

- ff6 knobs (the deployed/`demo_relaxed`-tested `ff6_4pct`, identical to R13 & the
  combined run): `failed-fade-exit-hours=6`, `failed-fade-loss-pct=0.04`,
  `failed-fade-min-mfe-pct=0.01`, `failed-fade-close-location-min=0.0`.
- **No rmom gate** (the whole point). All other knobs = validated `volume_events_cell.sh`
  defaults + the combined-run spec corrections: `max-active-symbols=12`, full-PIT,
  `bar_extreme_capped` 10% stop fill, 45 bps (×3 conservative cost).

## 4. Universe / data / window

- **Data roots:** `~/SHARED_DATA/bybit_full_pit`, `~/SHARED_DATA/binance_full_pit`
  (canonical research roots). Full-PIT required (engine hard-aborts partial-PIT).
- **Window:** 2023-04-01 → 2026-05-28 (matches E2 / R13 / the combined run).
  Early/recent split: 2025-06-01 (program convention) — both sub-periods, both venues.
- **Cost / fills:** 45 bps round-trip (×3), `bar_extreme_capped` 10% stop fill.
- **Funding:** bybit real per-trade; binance funding **missing** ⇒ binance is
  funding-blind/optimistic for a short — discount it, lean on bybit.

## 5. Run command(s)

```bash
TAG=age_ff6_2026-05-31
for V in bybit binance; do
  POLARS_MAX_THREADS=4 bash scripts/volume_events_cell.sh --venue "$V" \
    --cell-id age --phase "$TAG" --start 2023-04-01 --end 2026-05-28 \
    --overrides 'liquidity-migration-pit-age-days-min=300,max-active-symbols=12'
  POLARS_MAX_THREADS=4 bash scripts/volume_events_cell.sh --venue "$V" \
    --cell-id age_ff6 --phase "$TAG" --start 2023-04-01 --end 2026-05-28 \
    --overrides 'liquidity-migration-pit-age-days-min=300,max-active-symbols=12,failed-fade-exit-hours=6,failed-fade-loss-pct=0.04,failed-fade-min-mfe-pct=0.01,failed-fade-close-location-min=0.0'
done
.venv/bin/python scripts/r1_robustness.py --sweep-tag "$TAG" --control age
```

> Confirm every cell logs `run_label='full_pit_universe'` before trusting any number.

## 6. What gets reported (committed before seeing results)

- Trade count `age` vs `age_ff6` (identical by construction; report both).
- Return ×, max-DD, MAR, Sharpe — full window + early/recent thirds, both venues.
- **Exit histogram `age` vs `age_ff6`** — the count of `exit_failed_fade` (the mechanistic answer).
- Equity curve for `age_ff6` (the operator-requested artifact) + the per-trade ledger.
- The three-way verdict: ff6 ADDS / INERT / HURTS.

## 7. Falsifier / kill criteria

- **ff6 inert:** `exit_failed_fade` = 0 on both venues → ff6 adds nothing even without rmom;
  the combined-run conclusion generalizes (file "ff6 redundant on the age book too").
- **ff6 hurts:** pooled MAR Δ < 0 or a venue return-sign flip → drop ff6.
- **Recent-only / direction guards:** as in the combined receipt §7.

## 8. Provenance

- ff6 engine gate: `volume_events.py:1539` (`_failed_fade_exit_hit`); age: `volume_events_filters.py:756`.
- Prior evidence: R13 (ff6 on `drop_all_4`), E2 (age), `age-rmom-ff6-combined-2026-05-31.md` (ff6=0 on age+rmom).
- Box note: forced onto the 16 GB research box (swap-backed) per operator
  instruction; the 5950X was unavailable. Numbers verified `full_pit_universe` per cell regardless of box.

---

## RESULTS (filled 2026-05-31, post-run — §1–8 above are the pre-committed plan, untouched)

**Run:** tag `age_ff6_2026-05-31`, 2 cells × 2 venues, all `run_label='full_pit_universe'`
(`Full PIT universe pass: True`). Window resolved 2023-04-01 → 2026-05-27 (end exclusive).
Box: 16 GB research box, serial, `POLARS_MAX_THREADS≤6`. A single full-PIT cell fits at
~9 GB RSS with swap — RAM was NOT the blocker.

### Data-completion note (why the bybit cells first aborted)

The local `bybit_full_pit` klines root was one day short: 3 symbols entirely missing
(`AMDSTOCK/BE/WDC`, new listings) and 3 partial (`CHEEMS/DOG/HPOS10I`, 10<20 bars) — all on
**2026-05-28**, which is OUTSIDE the backtest window (last in-window day 2026-05-27). The
full-PIT gate (correctly) hard-aborted. Fixed by `archive-download-klines-1h-api` for those
6 symbol/days (now 24 bars each); this changed ZERO in-window trades. No gate loosening; no
`--allow-partial-pit`. Binance root was already complete (both cells passed first try).

### Metrics (full window; MAR = annualized-return / |maxDD| over the 3.15y span)

| venue | cell | trades | return | maxDD | Sharpe | MAR | `failed_fade` exits |
|---|---|---|---|---|---|---|---|
| bybit (funding real) | `age`     | 583 | +70.97% | −17.68% | 1.30 | **1.05** | 0 |
| bybit (funding real) | `age_ff6` | 585 | +78.51% | −17.43% | 1.43 | **1.16** | **29** |
| binance (funding blind) | `age`     | 307 | +21.95% | −12.59% | 0.71 | **0.52** | 0 |
| binance (funding blind) | `age_ff6` | 307 | +23.09% | −14.20% | 0.75 | **0.48** | **21** |

### Exit-reason migration (bybit age → age_ff6)

`event_decay` 336→323 (−13), `stop_loss` 159→145 (−14), `failed_fade` 0→**29**,
`max_hold`/`take_profit`/`data_end` unchanged; trades 583→585 (+2 from concurrency slots
freed by earlier ff6 exits — so entries are *near*-identical, not byte-identical, under
`max_active=12`).

### THE PRE-COMMITTED VERDICT (§2): ff6 **ADDS** (pooled), but venue-split

1. **NOT INERT.** ff6 catches 29 (bybit) / 21 (binance) trades on the age book vs **0** on
   age+rmom. Confirms the combined-run mechanism credit: it was **rmom** (pre-entry squeeze
   screen), not **age**, that left ff6 nothing to cut. age is a loose enough anti-squeeze
   filter that failed fades survive to entry.
2. **bybit (decisive, funding-real): ff6 ADDS.** MAR +0.11 (+10%), Sharpe +0.13, return
   +7.5pp, DD −0.25pp (slightly shallower). The R13 pattern. Mechanism: ff6 converts 14
   would-be −12% `stop_loss` exits + 13 would-be `event_decay` exits into earlier
   `failed_fade` cuts (~6–8h). The 29 ff6 trades are ALL losers (mean net −0.49%, win 0%,
   mean MAE −8.5%) — ff6 is pure loss-mitigation here, cutting squeeze-shorts that never
   worked (mean MFE +0.44%) before they ride to the hard stop.
3. **binance (funding-blind, discount): ff6 ~neutral/slightly-negative on MAR.** Return
   +1.1pp and Sharpe +0.04, but DD DEEPENED −12.59%→−14.20%, so MAR −0.04. Opposite of the
   bybit DD-shave.
4. **Pooled MAR Δ = +0.035 (> 0), neither venue negative, catches > 0 ⇒ ff6 ADDS** per the
   §2 rule — but the win is concentrated on the funding-real venue; binance dissents on DD.

### Bottom line

The combined-run "ff6 inert" conclusion was specific to the **rmom** book and does NOT
generalize to **age** (the deploy candidate). On bybit (funding-real), **ff6 + age stacks**
— a modest but real loss-mitigation lift (MAR 1.05→1.16). On binance it's a wash/slight-DD-
hit, but binance is funding-blind. Tier-2 in-sample only; the forward demo is the Tier-3
arbiter. Recommendation: ff6 is a reasonable add to the age deploy gate on Bybit; re-check
binance once a funding-complete binance root exists.
