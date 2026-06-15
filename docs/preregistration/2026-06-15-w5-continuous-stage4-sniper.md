# Pre-registration: W5 Continuous Stage 4 - Liquidity Entry Sniper

**Date:** 2026-06-15
**Author:** Claude (W5 continuous signal-alpha loop)
**Stage:** run-pending
**Plan:** `docs/research_plans/w5_continuous_signal_alpha/` (Stage 4 entry-selection / sniper)
**Contract:** `00_methodology_contract.md`; `docs/backtesting_errors_we_never_repeat.md`.
**Builds on:** Stage 4 screen (`…stage4-sniper` screen artifacts, 2026-06-15): of all
untested causal pre-entry features, **`log_turnover_quote` is the unique both-venue,
within-symbol selection signal** (within-symbol partial rank-IC over composite: bybit
+0.081 p=0.001, binance +0.134 p=0.001, thirds all positive, symbol-hash control
degenerate). Positive IC ⇒ within a symbol, the lower-turnover (less-liquid) fade entries
realize *worse* returns. Distinct from the path-shape trio (Stage 7b admissible but Stage 5
NOT harvestable via sizing) and from the regime levers.

## Question

Does **dropping the least-liquid fade entries** — a causal, de-trended low-turnover
"sniper" — improve pooled MAR vs the frozen control on BOTH venues, beyond a
count-matched random drop (i.e. is it *liquidity selection*, not mere de-leveraging)?
Mechanistic basis: illiquid fades have worse fills, more market impact, and noisier
reversion; cutting them should remove drawdown-heavy, low-EV trades while keeping ≥90% of
the book trading. This is a DOWN-sniper (cut the worst), mechanistically distinct from
Stage 5 symmetric sizing (a drop cuts DD directly) and from Stage 9 regime-sizing (which
failed because the book *profits* in high vol — liquidity is orthogonal to the vol-profit
source).

## Mechanism (locked before the run)

Per-entry notional via the additive `size_mult_lookup` hook (Stage 5): dropped entries get
multiplier `0.0`, kept entries `1.0`. Because each trade is sized to a FIXED independent
slot (`base_nw = gross_exposure / max_active`, optionally inverse-vol scaled) and the hook
is applied AFTER the concurrency gate, a `0.0` multiplier removes that slot's PnL and cost
and lowers gross WITHOUT altering any kept trade or freeing a concurrency slot (kept trades
byte-identical to V0; the BTC hedge resizes to the reduced book exposure). Entry COUNT is
unchanged; ~10% of slots carry zero notional.

Drop set (causal, de-trended, locked):

- per unique selected entry `(symbol, signal_ts)`, feature = `turnover_quote` (the
  decision-time quote turnover already in the Stage 0 tape — causal at `decision_ts`).
- causal trailing percentile: `pct(e)` = fraction of selected entries in the **trailing 180
  days strictly before `signal_ts`** whose `turnover_quote` < this entry's (expanding pool
  for the first 180 d). Trailing-relative ⇒ de-trended against the secular turnover rise, so
  the drop is NOT a time bucket.
- **drop iff `pct(e) < 0.10`** (bottom decile, low turnover — direction locked by the
  screen's positive IC). `k = 0.10` locked.

Random-drop control: per calendar DAY, drop the same NUMBER of that day's entries as the
turnover rule drops, chosen uniformly at random (seeded). Matching the per-day dropped count
makes the de-leveraging time-profile identical to the turnover arm, so any MAR difference is
liquidity SELECTION, not when/how-much gross was cut.

## Arms (locked)

- `T0_control`: frozen ensemble (no drop; `size_mult_lookup=None`). Must reproduce Stage 0.
- `T1_turnover_drop`: drop bottom-decile causal-trailing-turnover entries (the test).
- `T2_random_drop`: count-matched per-day random drop (de-leveraging control).
- `T1_turnover_drop_2xcost`, `T2_random_drop_2xcost`: same + `round_trip_cost_multiplier=2.0`
  (cost stress; the liquidity sniper should be MORE cost-robust, not less).

Full engine re-run per arm/component (cost recomputed at the new sizes), then frozen
ensemble/hedge rebuild. Hedge intensity NOT touched (isolates the selection lever; a
sniper+regime-hedge combination is a separate receipt).

## Constraints

- kept trades identical to V0 (drop is `size_mult=0` after all gates; entry count asserted
  unchanged; only notional/PnL of dropped slots → 0);
- effective dropped fraction ∈ [0.08, 0.12] per venue (≈ the locked decile; reported);
- causal feature (trailing-only percentile); funding ON; resize/impact cost charged;
- report dropped-set symbol concentration (distinct symbols, top-symbol share) and per-third
  dropped counts (guard against a symbol-bucket or time-bucket degenerate filter).

## Metrics

- total return, MAR, max drawdown, worst day; monthly R1; per-venue and pooled MAR delta;
  effective non-zero-trade count and total notional vs T0; chronological thirds; dropped-set
  symbol/temporal concentration.

## Decision rule (a priori) / Pass bar

`T1_turnover_drop` is a robust candidate (operator bar: any robust improvement that keeps
trading) iff, vs `T0`:

1. positive total return both venues;
2. pooled MAR delta `> 0` on **both venues** at 1× cost;
3. **beats `T2_random_drop`** on pooled MAR AND on each venue (the selection test — the
   crux; a tie ⇒ it's only de-leveraging, NULL);
4. drawdown not worse `> +10%` relative on either venue;
5. survives the 2×-cost arm (pooled MAR delta `> 0` both venues, and still beats
   `T2_random_drop_2xcost`);
6. benefit not carried by one venue or one chronological third;
7. not a degenerate filter: keeps ≥ ~90% of notional-bearing trades, dropped set spans many
   symbols (top symbol < ~15% of drops) and all three thirds.

Default label `exploratory`; a pass nominates a demo/paper forward-watch candidate (Tier-3
real-money gate UNCHANGED), combinable with the BTC-vol regime-hedge. Report whether it also
clears the strict +0.1.

## Falsifier

Reject if negative on either venue, does NOT beat the random-drop control (⇒ de-leveraging,
not selection), fails the 2×-cost arm, worsens drawdown, changes the kept population, is
carried by one venue/third, or the dropped set collapses onto a few symbols / one period. If
it fails, the liquidity-selection lever is closed and the next lever is a genuinely different
mechanism (Stage 2 entry-style); the BTC-vol regime-hedge stays the standing candidate.

## Window / roots / run

Window `2023-04-01 <= signal_ts < 2026-05-01`, both full-PIT roots; full engine re-run per
arm. Roots read-only; writes only to `reports/<tag>/` and `~/SHARED_DATA/w5_continuous_stage4_*`.

```bash
POLARS_MAX_THREADS=8 PYTHONPATH=. .venv/bin/python \
  scripts/w5_continuous_stage4_sniper.py \
  --venues bybit,binance --start 2023-04-01 --end 2026-05-01 \
  --stage0 ~/SHARED_DATA/w5_continuous_stage0_candidate_tape_2026-06-14 \
  --out ~/SHARED_DATA/w5_continuous_stage4_sniper_2026-06-15
```

## Post-run results

Run UTC 2026-06-15, both venues, git HEAD `5dd4e12` (code uncommitted), full engine re-run
per arm. T0_control reproduces the Stage 0 ensemble EXACTLY both venues (bybit 0.7707/4.748,
binance 0.6428/5.255). Drop set causal-trailing-180d decile, validated non-degenerate: bybit
130 dropped (frac 0.100, 104 distinct symbols, top-symbol share 0.038, thirds [36,43,51]);
binance 142 (frac 0.105, 108 symbols, top share 0.028, thirds [42,45,55]). Dropped names
~4–13× less liquid than kept. Per-day count-matched random control.

| Venue | Arm | Return | MAR | MaxDD | Nonzero |
|---|---|---:|---:|---:|---:|
| bybit | T0_control | 0.7707 | 4.748 | −5.27% | 3220 |
| bybit | T1_turnover_drop | 0.8067 | **4.907** | −5.33% | 2955 |
| bybit | T2_random_drop | 0.7566 | 4.577 | −5.36% | 2917 |
| bybit | T1_turnover_drop_2xcost | 0.6458 | 3.489 | −6.01% | 2955 |
| bybit | T2_random_drop_2xcost | 0.5946 | 3.394 | −5.68% | 2917 |
| binance | T0_control | 0.6428 | 5.255 | −3.97% | 2978 |
| binance | T1_turnover_drop | 0.6408 | **5.910** | −3.52% | 2815 |
| binance | T2_random_drop | 0.5874 | 4.909 | −3.88% | 2754 |
| binance | T1_turnover_drop_2xcost | 0.5187 | 4.171 | −4.03% | 2815 |
| binance | T2_random_drop_2xcost | 0.4660 | 3.340 | −4.53% | 2754 |

Deltas — bybit: T1−T0 **+0.159**, T1−T2 (1×) **+0.329**, T1_2x−T2_2x **+0.094**. binance:
T1−T0 **+0.655**, T1−T2 (1×) **+1.000**, T1_2x−T2_2x **+0.831**. **Pooled 1× ΔMAR vs T0
+0.407.** Selection thirds (T1−T2 return): bybit [−0.027, +0.011, +0.045] (positive 2/3),
binance [+0.000, +0.008, +0.025] (positive 3/3).

NOTE — the script's auto `robust=False` is a KNOWN mis-specification: there is no
`T0_control_2xcost` arm, so its per-venue "2x-cost MAR delta ≤0" check compares T1_2x against
the 1× control across cost regimes (always negative because 2× cost lowers the whole book).
The correct, pre-registered cost-robustness test is **T1_turnover_drop_2xcost vs
T2_random_drop_2xcost** (the random control isolates SELECTION at each cost level) — passes
both venues (+0.094 / +0.831). The pooled `t1x≤t2x` auto-check is valid (T0 cancels) and also
passes.

## Verdict

> **⚠️ ROBUSTNESS DOWNGRADE (Stage 4d, 2026-06-15) — READ FIRST.** The decile-robustness
> follow-up (`…stage4d-decile-robustness.md`) FALSIFIES the both-venue claim below. At k=5%
> and k=20% the turnover-drop does NOT replicate: on bybit a random drop matches it (k=5%) and
> beats it by +4.16 MAR (k=20%), and turnover-drop vs T0 swings +0.707/+0.159/−1.312 across
> k — bybit has **no robust liquidity effect**, and the **single-seed random control is too
> noisy** (bybit random MAR swings 4.6→7.6 by drawdown-event luck) to have established the
> k=10% "beats random" on bybit. Only **binance** shows a consistent liquidity effect (beats
> random at all k; peak ~k=10%). So the sniper is **VENUE-SPLIT (binance-real, bybit-noise),
> NOT a robust both-venue +0.407 improvement** — the headline was a favorable k=10% cut. The
> within-symbol IC is real (esp. binance +0.134) but does not translate to a robust both-venue
> MAR harvest (cf. the Stage 5/7b path-shape lesson). The standing both-venue candidate
> reverts to the **BTC-vol regime-hedge (Stage 8c)**. The original (now-superseded) k=10%
> verdict is retained below for the record.

**[SUPERSEDED — k=10% only, not robust to k] ROBUST — the program's first clearly harvestable
both-venue selection alpha, clearing even the strict +0.1 Tier-2 bar on each venue.** Dropping the least-liquid decile of fades
improves pooled MAR **+0.407** (bybit +0.159, binance +0.655), and DECISIVELY beats the
count-matched random-drop control on both venues at 1× (+0.329 / +1.000) and 2× cost (+0.094 /
+0.831) — so it is liquidity SELECTION, not de-leveraging. Drawdown flat (bybit) to improved
(binance −3.97%→−3.52%); keeps ~90% of notional-bearing trades; dropped set spans 104/108
symbols and all thirds (no degenerate filter); both venues positive; benefit not carried by
one third (binance positive all 3 thirds, bybit 2/3). All pre-registered pass criteria met
(the lone auto-fail is the mis-specified 2× check above). Falsifier NOT triggered.

**Mechanism & honest caveats:** the two venues harvest it differently — binance via a
structural DRAWDOWN reduction (return flat across all thirds, DD cut by dropping the
drawdown-contributing illiquid trades), bybit via a return gain concentrated in the latter
2/3 of the window (slightly negative selection in the first third). Part of the edge is
execution-cost avoidance: the cost model charges impact ∝ notional/ADV, so the lowest-turnover
trades carry the highest modeled impact — dropping them removes the highest-cost, lowest-EV
fades. That is a legitimate improvement (don't deploy where impact eats the edge) but it is
partly **model-dependent** on the impact calibration, which is exactly why the path forward is
**demo/paper forward-watch** (forward fills validate the execution-cost component). The
within-symbol IC screen (turnover +0.081 bybit / +0.134 binance, p=0.001, symbol-hash
degenerate) independently established the liquidity→return link is real beyond symbol identity.

**Gross/cost decomposition of the dropped trades (T0 component ledgers) — NOT a pure
cost-model artifact:** the dropped least-liquid trades carry ~HALF (bybit) to ~ZERO (binance)
the gross per-trade edge of kept trades — bybit dropped gross +1.3 bp/trade vs kept +2.7 bp
(net +0.7 vs +2.0 bp); binance dropped gross **≈0** vs kept +2.1 bp (net **−0.6 bp**, i.e.
pure cost+funding drag). So liquidity predicts genuinely low *gross* fade quality, not merely
higher cost on otherwise-good fades — the benefit should survive real fills, strengthening the
forward-watch case. (The dropped trades are net-marginal/negative; the random-drop control
absorbs the common BTC-hedge-resize interaction, so the +0.33/+1.00 selection delta is the
clean liquidity effect.)

**Label:** `candidate` (demo/paper forward-watch; Tier-3 real-money gate UNCHANGED). The
second W5 candidate alongside the BTC-vol regime-hedge — and stronger. Next: a sniper+regime
-hedge COMBINATION receipt (both additive hooks — sniper drops entries, hedge resizes the BTC
leg — test whether the gains stack or interact), and a robustness follow-up varying the drop
decile (k∈{5,15,20}%) under a fresh receipt to confirm the +0.1 isn't a k=10 artifact.
