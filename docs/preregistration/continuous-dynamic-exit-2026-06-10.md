# Pre-registration: fade-completion dynamic exit vs the fixed-TP/24h clock — §8-P5, one shot (2026-06-10)

**Charter:** §4-D / §8-P5 (`docs/research_plan_alpha_hunt_2026-06-10.md`). Honest prior:
LOW — the exit family has a deep pre-registered graveyard (TP grid, hold extension,
trailing/ATR stops, scaled exits, MFE giveback, breakeven, rank-decay, hard stops,
half-life timing, settlement-aware exits; multi-horizon showed 24h is THE cross-venue
horizon). The ONE untested member: a per-event **fade-completion target** — the fixed
TP ignores that a 30% pump has more fade room than a 5% one. Per the charter: a single
falsifier-first shot; **a NULL closes §4-D permanently.** Design + bars frozen BEFORE
any treatment cell. Run label: `exploratory`. Single shot, no grids.

## Frozen design

- **Treatment exit:** each winner_base component trade's fixed TP (TP10/TP14) is
  replaced by a dynamic target: `tp_price = entry_price × (1 − f · anchor)` with
  **f = 0.5** and `anchor = max(runup24h, ret1)` at the SIGNAL bar (causal:
  close[sig]/close[sig−24h] − 1 and the trigger's own 1h pop; anchor clipped to
  [0.03, 0.60]). Everything else identical: 24h max hold, no stops, same entry, same
  cost basis (entry-day round-trip costs unchanged — conservative, the exit is a limit
  the engine also assumed), funding scaled linearly by hold-time ratio (stated
  approximation; per-trade funding is ~0.004% — second-order).
- **Fill mechanics mirror the engine exactly** (`_bar_exit_hits` semantics): walk 1h
  bars from entry; short TP fills AT tp_price when bar low ≤ tp_price, stamped at bar
  END; max-hold exit at the 24h bar close. Bar data from the venue's `klines_1h`
  partitions (full-PIT roots).
- **Control parity gate (T0):** the SAME re-simulator run with the ORIGINAL fixed TP
  must reproduce the ledger's exits (≥98% of trades matching exit price+timestamp
  within float tolerance) AND the rebuilt control book must pass the standard parity
  band vs the official combined control (corr ≥ 0.995, |ΔMAR| ≤ 0.3, |ΔSharpe| ≤ 0.10,
  |Δret| ≤ 5pp). Fail ⇒ INVALID, stop — the comparison is treatment-vs-rebuilt-control
  on identical machinery.
- **Book mechanics:** re-simulated trades → the parity-verified per-trade daily-split →
  component rebuild → frozen winner weights → `apply_rebalance_rule` @ max4, both
  venues, 1× and 2× cost.
- **Sensitivity (reported, pre-declared NON-selecting):** f = 0.75 row.

## A-priori bars

- **T0** parity (above). **T1** positive return both venues. **T2** pooled MAR-Δ >
  +0.1 AND per-venue MAR-Δ > −0.5 vs rebuilt control. **T3** T1+T2 at 2× cost.
  **T4** pooled ΔSharpe > 0. **PASS = all.** Fragility (thirds, exit-reason mix,
  mean hold change) reported, non-blocking.

**NULL ⇒ §4-D is CLOSED PERMANENTLY** (the charter's pre-commitment): the 24h clock +
fixed TP is exit-optimal at this granularity; no further exit work on this window by
any future agent without fundamentally new data (e.g. live order-book state).
**Pre-stated failure modes:** big-pop names fade in chunks with violent retraces — a
scaled target may exit too late (f·anchor beyond the realizable fade) turning TP wins
into max-hold draws; the TP grid already showed the fixed-percent surface is FLAT
(TP10≈12≈14≈16), which weakly predicts target-form insensitivity ⇒ NULL.

## Artifacts

Driver `scripts/continuous_dynamic_exit_driver.py`; out
`C:\Users\user\SHARED_DATA\continuous_dynamic_exit_2026-06-10\`. Verdict appended here
+ roll-ups in `docs/research_summary.md`, STATE.md, memory.

---

## VERDICT (run 2026-06-10, same day, design unchanged): **NULL — §4-D CLOSED PERMANENTLY**

**T0 parity: PERFECT** — the bar-accurate replay reproduced the ledger's exits
**100.0%** at trade level on BOTH venues (and the rebuilt control matched the official
book: corr 0.9999/0.9998). The exit re-simulator is now a verified primitive (reusable
for §8-P2 execution work — that was the dual purpose of building it properly).

| venue | dyn ret | ΔMAR | ΔSharpe | ΔMAR @2× | TP-exit frac ctrl→dyn | mean hold ctrl→dyn |
|---|---:|---:|---:|---:|---|---|
| bybit | +112.97% | **+1.74** | +0.485 | +1.36 | .31→.33 | 20.2→19.9h |
| binance | +44.98% | **−2.10** | −0.300 | −1.63 | .30→.44 | 20.0→17.2h |

Pooled ΔMAR **−0.18** (bar +0.1) → T2/T3 FAIL → NULL.

**The teaching value of this receipt is the mirage:** a +1.74 MAR / +0.49 Sharpe
improvement on bybit — which a single-venue process would have shipped — is fully
sign-reversed on binance (DD blows 4.3→6.0%). The fade's realizable depth profile is
venue-specific: binance's dynamic targets fill far more often (.44 vs .30) yet
risk-adjusted results collapse — the percent-of-pop target exits the fade body too
early there while keeping the squeeze tail. This is the strongest in-house
demonstration to date of the house rule that cross-venue agreement IS the robustness
test. Echoes the multi-horizon null (bybit-only horizon effects).

**Per the pre-commitment: §4-D (dynamic/vol-scaled exits) is CLOSED PERMANENTLY on
this window.** The 24h clock + fixed TP stands. No future agent should re-open exits
absent fundamentally new data (live order-book state / calibrated fills). The f=0.75
diagnostic (closer to fixed-TP behavior) confirms the gradient: bybit +5.82 MAR /
binance 3.31 — the venue disagreement is structural, not an f-tuning artifact.

## FORENSIC ADDENDUM (2026-06-10, operator-requested; descriptive only — `p5_venue_divergence_diag.py`, artifacts `diag_trades_{venue}.parquet` + `venue_divergence_diag.json`)

Matched per-trade control-vs-dynamic ledgers, both venues. Findings:

1. **The trade populations are near-identical.** Anchor (pump-size) distributions:
   bybit median 16.3% / binance 14.5%; dyn target shallower than the fixed TP for 68%
   (bybit) / 73% (binance) of trades. The divergence is NOT in what was traded.
2. **The within-venue logic of the dynamic exit is the SAME on both venues** — delta
   monotone in anchor: small-anchor trades hurt (−25 bybit / −85 binance bps),
   large-anchor trades helped (+118 / +57). The exit idea is coherent; the venues
   price it differently.
3. **The divergence is the post-TP continuation profile** (transition matrix, mean
   per-unit Δ): tp→tp (shallower target, exit earlier) −90 bybit / −130 binance;
   max_hold→tp (new winners) +208 / +150; and the killer, **tp→max_hold (deeper
   target missed, ride past the old TP10 point): +40 bybit vs −261 binance** — on
   bybit the fade KEEPS FADING beyond −10% from entry; on binance the fade is
   exhausted at ~7-10% and BOUNCES. A ~300bps/trade venue asymmetry on the
   deepest-target trades. Consistent with CV1 (bybit's edge = its thin/exclusive
   tail, which collapses further) and the participation-cap finding.
4. **Honesty check on the bybit headline: it is 2026-carried.** Per-year mean Δ:
   2023 −7bps, 2024 +17, 2025 +17, **2026 +94**. Binance is negative EVERY year
   (−10..−20) — its failure is structural. So even the attractive half of the mirage
   is substantially a recent-regime phenomenon (same recency pattern as the OI IC).
5. Post-hoc observation, recorded NOT runnable: a large-anchor-only hybrid would have
   been positive on both venues per (2) — but that is a conditional constructed AFTER
   seeing the result, exactly what the §4-D closure exists to prevent; it could only
   ever be tested as a FORWARD shadow, never in-sample.
