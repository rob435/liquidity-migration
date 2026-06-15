# Pre-registration: W5 Continuous Stage 3b - Hold-Extension Exit (longer hold)

**Date:** 2026-06-14
**Author:** Claude (W5 continuous signal-alpha loop)
**Stage:** run-pending
**Plan:** `docs/research_plans/w5_continuous_signal_alpha/04_stage3_exit_alpha.md`
**Contract:** `docs/research_plans/w5_continuous_signal_alpha/00_methodology_contract.md`
**Motivated by:** Stage 3
(`docs/preregistration/2026-06-14-w5-continuous-stage3-exit-alpha.md`) — an MFE-giveback
*earlier* exit DESTROYED return (pooled MAR −2.29, worse than random), because for a
mean-reversion fade book, positions at the 24h max-hold cap are on average still
favorable/reverting. The clean, opposite-direction follow-up: does holding **longer**
capture more reversion (and more funding credit) than the fixed 24h hold?

## Question

Does extending the fixed hold (`hold_hours` 24 → 48) on the SAME entries improve pooled
MAR vs the frozen control on both venues — i.e. is the 24h cap leaving reversion (and
short-funding credit) on the table — or does the extra exposure to alt squeezes worsen
drawdown enough to cancel it?

## Mechanism (locked before the run)

The four frozen components are `exit_mode="fixed"`, `hold_hours=24`, fixed-TP (tp10/tp14)
+ 24h max-hold. The smoke confirmed most exits hit the 24h cap (avg hold ~20h). Stage 3b
overrides `hold_hours` (config override via `_component_config(arm_overrides=...)`),
leaving entries / gates / sizing / TP / hedge identical — so the entry population is
unchanged and only the hold cap moves. Each arm is a full engine re-run of the four
components per venue, then the frozen ensemble/hedge rebuild.

**Locked: primary extension `hold_hours = 48` (2×).** A priori, not tuned.

## Arms (locked)

- `X0_control`: `hold_hours=24` (frozen) — the Stage 0 ensemble.
- `XH_hold48`: `hold_hours=48`.
- `XH_hold48_2xcost`: `hold_hours=48` + `round_trip_cost_multiplier=2.0` (cost-stress;
  longer holds also accrue more funding, surfaced in the ledger).
- `XH_hold48_hashplacebo` (negative control): `hold_hours=48` + `hash_exit_prob=0.018`
  per bar — a 48h cap with deterministic no-content random early exits (additive hook,
  default 0 → byte-identical; 551 tests pass), pulling the realized hold back toward the
  control with RANDOM timing. Tests whether a *systematic* longer hold beats random hold
  variation at a comparable average. Realized avg holds reported (the rate is locked,
  not matched).

## Constraints (binding)

- same ENTRIES as X0 (only the hold cap changes — entry count asserted identical);
- causal (the hold cap is a fixed forward duration, no future info);
- funding ON (the extra hold's funding is charged/credited through the engine);
- resize/impact cost charged; NOT a stop / failed-fade / breakeven overlay.

## Metrics (per arm, per venue, pooled)

- total return, MAR, max drawdown, worst day; R1-compatible monthly returns;
- average hold (hours); exit-reason distribution; entry count vs X0;
- funding_return contribution (longer hold ⇒ more funding); per-component attribution;
- chronological-third MAR-delta stability.

## Decision rule (a priori) / Pass bar

`XH_hold48` is admissible only if, vs `X0`:

1. positive total return on **both** venues;
2. **pooled MAR delta `> +0.1`**;
3. no venue MAR delta `< -0.5`;
4. max drawdown / worst-day not worse than **+10% relative** on either venue (the
   longer-hold squeeze-risk guard);
5. survives `XH_hold48_2xcost` (still pooled MAR delta `> +0.1`);
6. the `XH_hold48_hashplacebo` control pooled MAR delta is strictly weaker;
7. entry count identical to X0 on both venues (same entries);
8. not carried by one venue or one chronological third.

Default label **`exploratory`** — historical. A pass nominates a demo/paper exit
shadow only.

## Falsifier

Reject if it works on one venue only, is matched/beaten by the hash-placebo, fails the
2x-cost arm, worsens drawdown beyond tolerance (longer holds = more squeeze exposure),
changes the entry population, or the MAR gain lives in one chronological third.

**Program-completion note:** if Stage 3b also misses the +0.1 bar, the W5 program has
tested the exit lever in both directions (earlier=Stage 3 harmful; longer=Stage 3b) and
the major levers (entry-priority, path-shape, sizing, regime-hedge, exits) all
converge on the control being near-optimal — the program is then effectively complete;
bank that and stop queuing marginal levers (the Stage 8 regime-hedge ~+0.05 is the best
forward-watch candidate). A score-conditioned hold (X3, per-entry) is a distinct future
idea only if Stage 3b shows uniform longer holds help.

## Window, roots, universe

Window `2023-04-01 <= signal_ts < 2026-05-01`, both full-PIT roots; full engine re-run
per arm. Roots read-only; writes only to `reports/<tag>/` and
`~/SHARED_DATA/w5_continuous_stage3b_*`.

## Run command

```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
  scripts/w5_continuous_stage3b_hold_extension.py \
  --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
  --out ~/SHARED_DATA/w5_continuous_stage3b_hold_extension_2026-06-14
```

## Artifacts

Under `~/SHARED_DATA/w5_continuous_stage3b_hold_extension_2026-06-14/` and per-venue
`reports/w5_continuous_stage3b_hold_extension_2026-06-14/{arm}/{component}/`: per-arm
ensemble ledger + monthly + report JSON (R1-compatible); `stage3b_summary.{json,md}`,
`stage3b_metrics.csv`, `exit_reasons.csv`, `code_hash.txt`.

## Post-run results

Run UTC 2026-06-14, both venues, window `2023-04-01 <= signal_ts < 2026-05-01`, git
HEAD `5dd4e12` (code uncommitted; code hash `fbd037f5…`), frozen config hash
`1fc760f1…`. X0 reproduces the Stage 0 ensemble exactly (bybit 0.7707/4.748, binance
0.6428/5.255). Artifacts `~/SHARED_DATA/w5_continuous_stage3b_hold_extension_2026-06-14/`.

| Venue | Arm | Entries | Return | MAR | MaxDD | Avg hold (h) |
|---|---|---:|---:|---:|---:|---:|
| bybit | X0 | 3220 | 0.7707 | 4.748 | −5.27% | 20.3 |
| bybit | XH_hold48 | 3089 | 0.9809 | 4.871 | −6.53% | 36.0 |
| bybit | XH_hold48_2xcost | 3089 | 0.8164 | 3.765 | −7.03% | 36.0 |
| bybit | XH_hold48_hashplacebo | 3089 | 0.4594 | 1.887 | −7.89% | 24.8 |
| binance | X0 | 2978 | 0.6428 | 5.255 | −3.97% | 20.7 |
| binance | XH_hold48 | 2857 | 0.3626 | 1.715 | −6.85% | 37.6 |
| binance | XH_hold48_2xcost | 2857 | 0.2803 | 1.303 | −6.97% | 37.6 |
| binance | XH_hold48_hashplacebo | 2857 | 0.1643 | 0.793 | −6.72% | 25.9 |

Pooled MAR delta vs X0: XH_hold48 **−1.708** (bybit **+0.123**, binance **−3.540**);
2x-cost −2.467; hash-placebo −3.661. **Confound (disclosed):** in fixed mode the
re-entry cooldown equals `hold_hours`, so 24→48h also doubled the cooldown and dropped
entries ~4% (bybit 3220→3089, binance 2978→2857) — the arm is longer-hold *plus*
longer-cooldown, not a pure exit change. Avg hold rose 20→36–38h as intended.

## Verdict

**NULL — and the exit lever is now closed in both directions.** Extending the hold to
48h DOES capture more reversion on bybit (return 0.771→0.981 — confirming Stage 3's
insight that positions are still reverting at the 24h cap), but the extra exposure
worsens drawdown on both venues (bybit −5.27→−6.53%, binance −3.97→−6.85%), so bybit
MAR barely moves (+0.12) while **binance collapses** (5.255→1.715, −3.54) — alt
squeezes over the longer hold do far more damage on binance. Pooled MAR delta
**−1.708**, decisively split-venue, DD worse both. The systematic longer hold does
beat the random hash-placebo (−3.66), so hold *timing* carries some structure, but
both are far below control. Fails the strict +0.1 bar AND the looser
robust-improvement bar (single-venue at best, DD worse, not robust, reduced breadth).

Combined with Stage 3 (earlier exits harmful), the **fixed 24h hold is near-optimal** —
it sits at the reversion-capture vs squeeze-risk balance, and moving the hold either
direction worsens risk-adjusted return. Falsifier outcome: **triggered** (split venues,
DD worse, fails 2x-cost, breadth changed). A cooldown-decoupled longer-hold or a
score-conditioned per-entry hold (X3) could isolate the exit effect, but given the
−3.54 binance crash the exit lever is not a promising robust-edge direction. Next:
firm up the regime-hedge (the best lead) per operator guidance, then new mechanisms.
