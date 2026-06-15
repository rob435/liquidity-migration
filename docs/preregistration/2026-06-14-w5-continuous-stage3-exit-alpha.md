# Pre-registration: W5 Continuous Stage 3 - Exit Lifecycle Alpha (MFE-giveback trail)

**Date:** 2026-06-14
**Author:** Claude (W5 continuous signal-alpha loop)
**Stage:** run-pending
**Plan:** `docs/research_plans/w5_continuous_signal_alpha/04_stage3_exit_alpha.md`
**Contract:** `docs/research_plans/w5_continuous_signal_alpha/00_methodology_contract.md`
**Depends on:** Stage 0 PASS.
**Binding prior:** W4 Stage 1 (`docs/preregistration/2026-06-13-w4-continuous-stage1-stop-exit-realism.md`)
CLOSED the exact 25% disaster stop + failed-fade + breakeven overlay. This stage does
NOT reuse stops, failed-fade, or breakeven.

## Question

Can a causal **trailing gain-lock exit** (MFE-giveback) improve pooled MAR vs the
frozen fixed-TP / max-hold lifecycle, on both venues — by cutting the
gains-that-revert tail that drives the book's drawdown — without repeating the
W4-closed stop overlay?

## Mechanism (locked before the run)

The frozen control exits each fade on the FIRST of: fixed take-profit (component
tp10/tp14) or max-hold (age240/age210). Stage 3 adds, *for the same entries*, the
already-wired causal **MFE-giveback** trail (`continuous_events._simulate_indexed_trade`,
exit reason `mfe_giveback`): once the position's favorable excursion (MFE) reaches
`mfe_giveback_trigger_pct`, exit at the bar close if the close return falls back to
`mfe_giveback_retain_pct × MFE`. It is fully causal (MFE-so-far and close are known at
the bar) and live-warm-startable (MFE is tracked from entry; no future path, no
end-of-trade label).

**Locked params:** `mfe_giveback_trigger_pct = 0.05`, `mfe_giveback_retain_pct = 0.50`
— arm the trail after a 5% favorable move, exit on a 50% giveback of the peak gain.
Set a priori (arm below the 10–14% TP so the trail can act before TP); NOT tuned on
the data. A different parameterization is a separate future receipt.

This changes the trade *population/returns* (exits move), so each arm is a full
engine re-run of the four frozen components per venue (config override via
`_component_config(arm_overrides=...)`), then the frozen ensemble/hedge rebuild — NOT
a component-reuse shortcut.

## Arms (locked)

- `X0_control`: frozen exits (`mfe_giveback` off) — the Stage 0 ensemble.
- `XT_mfe_giveback`: trigger 0.05 / retain 0.50.
- `XT_mfe_giveback_2xcost`: same + `round_trip_cost_multiplier = 2.0` (cost-stress).
- `X5_hash_exit` (negative control): `hash_exit_prob = 0.004` per bar — a
  deterministic per-(symbol,bar) hash exit with **no market content**, additive engine
  hook (default 0 → byte-identical; 551 tests pass). Tests whether the MFE-giveback's
  path-based timing beats a no-content early exit at a comparable rate (realized exit
  rates / avg holds reported for both; the hash rate is locked, not matched).

## Constraints (binding)

- same ENTRIES as V0 (only the exit rule changes — entry gates/sizing/crowding
  identical; asserted by the entry count vs X0 staying within data drift);
- causal, live-warm-startable exit state only (no future MFE/MAE/rank/label);
- funding ON (bybit modeled / binance partial-disclosed); resize/impact cost charged;
- NOT the W4-closed stop / failed-fade / breakeven overlay.

## Metrics (per arm, per venue, pooled)

- total return, MAR, max drawdown, worst day; R1-compatible monthly returns;
- average hold (hours); **exit-reason distribution** (take_profit / mfe_giveback /
  max_hold / hash_exit / data_end); entry count vs X0 (same-entries check);
- per-component attribution; chronological-third MAR-delta stability.

## Decision rule (a priori) / Pass bar

`XT_mfe_giveback` is admissible only if, vs `X0`:

1. positive total return on **both** venues;
2. **pooled MAR delta `> +0.1`**;
3. drawdown OR worst-day improves on at least the venue carrying the MAR gain, and
   does not worsen `> +10%` relative on either venue (the mechanism's stated job is
   tail/DD reduction);
4. no venue MAR delta `< -0.5`;
5. survives `XT_mfe_giveback_2xcost` (still pooled MAR delta `> +0.1`);
6. the negative control `X5_hash_exit` pooled MAR delta is strictly weaker;
7. entry count within data-drift of X0 on both venues (same entries — not a hidden
   selection change);
8. not carried by one venue or one chronological third.

Default label **`exploratory`** — historical. A pass nominates a demo/paper exit
shadow only; the frozen lifecycle remains the live control until a Tier-3 forward
verdict.

## Falsifier

Reject if it works on one venue only, is matched/beaten by the hash control, fails
the 2x-cost arm, destroys return for the drawdown reduction (MAR falls), changes the
entry population, or would require future path / non-warm-startable state. If
XT_mfe_giveback misses the bar, the next exit lever (rank/score-decay exit X1, or a
score-conditioned time cap X3) is a separate future receipt.

## Window, roots, universe

Window `2023-04-01 <= signal_ts < 2026-05-01`, both full-PIT roots; full engine
re-run per arm. Roots read-only; writes only to `reports/<tag>/` and
`~/SHARED_DATA/w5_continuous_stage3_*`.

## Run command

```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
  scripts/w5_continuous_stage3_exit_alpha.py \
  --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
  --out ~/SHARED_DATA/w5_continuous_stage3_exit_alpha_2026-06-14
```

## Artifacts

Under `~/SHARED_DATA/w5_continuous_stage3_exit_alpha_2026-06-14/` and per-venue
`reports/w5_continuous_stage3_exit_alpha_2026-06-14/{arm}/{component}/`: per-arm
ensemble ledger + monthly + report JSON (R1-compatible); `stage3_summary.{json,md}`,
`stage3_metrics.csv`, `exit_reasons.csv`, `code_hash.txt`, `config_hashes.json`.

## Post-run results

Run UTC 2026-06-14, both venues, window `2023-04-01 <= signal_ts < 2026-05-01`,
git HEAD `5dd4e12` (engine hook + Stage 3 code uncommitted; code hash `2b5a563e…`),
frozen config hash `1fc760f1…`, mfe_giveback trigger 0.05 / retain 0.50. Artifacts
`~/SHARED_DATA/w5_continuous_stage3_exit_alpha_2026-06-14/`. X0 reproduces the Stage 0
ensemble exactly (bybit 0.7707/4.748; binance 0.6428/5.255); **entry counts identical
across all arms** (bybit 3220, binance 2978) — exits do not change the population.

| Venue | Arm | Entries | Return | MAR | MaxDD | Avg hold (h) |
|---|---|---:|---:|---:|---:|---:|
| bybit | X0 | 3220 | 0.7707 | 4.748 | −5.27% | 20.3 |
| bybit | XT mfe_giveback | 3220 | 0.5349 | 3.744 | −4.63% | 15.6 |
| bybit | XT 2x-cost | 3220 | 0.2730 | 1.604 | −5.52% | 15.6 |
| bybit | X5 hash | 3220 | 0.5937 | 3.118 | −6.18% | 19.3 |
| binance | X0 | 2978 | 0.6428 | 5.255 | −3.97% | 20.7 |
| binance | XT mfe_giveback | 2978 | 0.2576 | 1.675 | −4.99% | 16.6 |
| binance | XT 2x-cost | 2978 | 0.0773 | 0.407 | −6.16% | 16.6 |
| binance | X5 hash | 2978 | 0.6264 | 5.041 | −4.03% | 19.8 |

Pooled MAR delta vs X0: XT **−2.291** (bybit −1.004, binance −3.580); 2x-cost
−3.996; X5 hash −0.922.

## Verdict

**NULL — decisively harmful, and informative.** The MFE-giveback trailing gain-lock
exit DESTROYS return on both venues (bybit 0.771→0.535, binance 0.643→0.258) for a
small/mixed drawdown change (bybit DD slightly better −5.27→−4.63%, binance WORSE
−3.97→−4.99%), collapsing pooled MAR by **−2.29**. Critically, **mfe_giveback is
WORSE than the random hash-exit control** (−2.29 vs −0.92) — the gain-lock *timing*
is actively counterproductive, not merely neutral.

The mechanism is clear and a genuine lesson: this is a **mean-reversion fade book**
(short overextended alts, expecting reversion to the fixed TP). A trailing gain-lock
exits on the first meaningful *bounce* (a 50% giveback of the peak favorable move) —
but for a reverting position a bounce is noise that typically precedes *continued*
reversion. So mfe_giveback systematically bails right before the trade would complete,
cutting the winner (avg hold 20→16h, ~25% shorter) and forgoing the bulk of the
reversion. The fixed-TP/max-hold control is well-matched to the thesis; exiting
*earlier* fights it. (That mfe_giveback < the random control confirms the early-exit
*timing* is specifically bad, not just the shorter hold.)

**Banked implication for the exit lever:** *earlier* exits harm this fade book — any
path/signal-decay exit that shortens holds (mfe-giveback here; X1 rank-decay likely
the same) will hurt for the same reason: cutting before full reversion. The
fixed-hold-to-TP exit is near-optimal. A *longer*-hold / score-conditioned-time-cap
exit (X3) is the only remaining distinct exit direction and is lower-prior given the
control's fit. Falsifier outcome: **triggered** (negative both venues, beaten by the
random control, fails 2x-cost). Next lever: a different stage (Stage 2 entry-style /
Stage 4 sniper).
