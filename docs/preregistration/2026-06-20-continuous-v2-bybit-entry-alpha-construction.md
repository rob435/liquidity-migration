# Construction + Result: Bybit Entry-Alpha — Exhaustion Features (operator direction 2026-06-20)

Date: 2026-06-20
Author: Claude (operator-directed; Bybit-only entry-alpha push)
Run label: `exploratory` (OOS screen). **Result: a real, diversification-preserving, OOS, hash-beating Bybit entry-alpha improvement — sizing UP the high-upper-wick (exhaustion/rejection) entries lifts MAR proxy +3.0. Admission fails (concentration kills MAR). Needs full-ledger confirmation via the engine's size_mult_lookup before any candidate claim.**

## Approach (honest creative mining)

Bybit only; perfect the ENTRY. Theory of the fade: short a pop, profit on reversion → the
best entries are where the pop is EXHAUSTING (climactic, rejected, over-extended) not
trending. Built a rich causal pre-entry 1m feature library and ranked by IC vs realized
gross with a TIME SPLIT (early/late) for stability; then AB-tested admission/sizing OUT OF
SAMPLE (features standardized on the early window, rule applied to the late window) vs hash
nulls. Mining is allowed; fooling ourselves is not — hence OOS + hash + theory-driven
feature choice. Scripts: `continuous_v2_bybit_entry_alpha.py` (stage 1 IC),
`continuous_v2_bybit_entry_ab.py` (stage 2 AB + MAR proxy).

## Stage 1 — feature IC vs realized gross (Bybit V2_CONTROL, n=2367; stable across early/late)

| feature | IC gross | IC vs MAE | note |
|---------|---------:|----------:|------|
| rv_30 | +0.24 | −0.21 | risk-coupled (scary=best) |
| range_expansion | +0.18 | −0.21 | risk-coupled |
| run_up_120 | +0.17 | −0.21 | risk-coupled |
| vol_climax | +0.15 | −0.11 | partly risk-coupled |
| **upper_wick_mean** | +0.15 | **−0.005** | CLEAN: better trades, ~no extra tail |
| ext_from_vwap | +0.12 | −0.08 | exhaustion |
| pv_divergence | +0.12 | +0.01 | exhaustion |

The clean ENTRY edge is exhaustion features that lift gross WITHOUT raising MAE; the
rv_30/range/run_up cluster lifts gross by taking more risk (sizing's job, not entry's).

## Stage 2 — OOS AB (late window n=894; control mean trade +0.0401, MAR proxy 14.19)

| arm | OOS mean (Δ) | n | MAR proxy (Δ) |
|-----|-------------:|--:|--------------:|
| admit50_EXHAUST | +0.0474 (+0.0073) | 447 | 7.69 (**−6.50**) |
| admit67_EXHAUST | +0.0463 (+0.0062) | 598 | 12.49 (−1.69) |
| **size_upper_wick_mean** | +0.0416 (+0.0015) | 894 | **17.19 (+3.00)** |
| size_EXHAUST (composite) | +0.0430 (+0.0030) | 894 | 11.87 (−2.32) |
| size_HASH (null) | +0.0406 (+0.0006) | 894 | 14.66 (+0.47) |
| admit67_HASH (null) | +0.0379 (−0.0022) | 598 | 7.50 (−6.69) |

## Findings

1. **Admission is a per-trade illusion.** The exhaustion composite admission has the best
   per-trade mean (+0.0073 OOS) but its MAR proxy COLLAPSES (−6.50) — dropping half the
   trades concentrates the book. The Book B lesson, reconfirmed: judge entry rules on
   portfolio MAR, not per-trade mean. Admission is OUT.
2. **Sizing UP by upper_wick is a real, clean win.** It keeps all trades (no diversification
   loss), improves the OOS mean (+0.0015) AND the MAR proxy (+3.0), and BEATS its hash null
   (+3.0 vs +0.47). Mechanism: upper_wick (+0.15 gross IC, ≈0 MAE IC) selects better trades
   without adding tail, so sizing up raises return without raising drawdown → MAR up.
3. **upper_wick alone beats the composite for sizing.** The composite's vol_climax /
   ext_from_vwap are risk-coupled and drag MAR (size_vol_climax MAR −4.34). The cleanest
   single signal wins — don't dilute it.

## Status & next (NOT yet a candidate)

- This is an OOS SCREEN on a rough daily MAR proxy with equal-base sizing. Before any
  candidate claim it needs FULL-LEDGER confirmation: run upper_wick sizing through the real
  engine via `run_continuous_event_research(size_mult_lookup=...)` + `build_full_ledger`
  (the rebalance/hedge re-solve), with the prior B-sizing discipline (beat the matched hash
  at the full-ledger level, not just the proxy). The prior both-venue B1 sizing was beaten
  by hash at full-ledger — so this must clear that bar.
- Bybit-only by construction (operator focus). Even if confirmed it is an operator-gated
  per-venue lead, not a frozen-object change.

## Dynamic-TP push (upper_wick is the unifying signal)

`scripts/continuous_v2_bybit_dyntp.py` (OOS, Bybit, equal-weight). Continuing "keep
pushing dynamic TP" — two new path-/feature-aware ideas:

| policy | OOS mean (Δ vs flat TP12) | beats hash |
|--------|-------------------------:|:----------:|
| **uw_cond_12to15** (top-tercile upper_wick → TP15, else TP12) | +0.04146 (**+0.0014**) | **yes** (hash −0.00001) |
| speed_arm4_t90 (reversion-speed-adaptive) | +0.04041 (+0.0004) | marginal |
| flat_TP15 | +0.03925 (−0.0008) | — |
| reversion-speed-adaptive (other variants) | −0.0009 to −0.0023 | no |

- **upper_wick-conditional TP works** (+0.0014 OOS, beats hash): high-exhaustion fades
  revert further and earn a wider TP, while the rest keep the tight TP. Flat TP15 still
  LOSES on Bybit (−0.0008) — uniform widening hurts; upper_wick identifies WHICH trades
  deserve it.
- **Reversion-speed-adaptive TP fails** — pre-entry exhaustion (upper_wick) beats path-speed
  as the signal.
- So `upper_wick` improves BOTH entry sizing (MAR proxy +3.0) AND TP conditioning (+0.0014,
  beats hash) OOS — one coherent, mechanism-backed Bybit edge (exhaustion/rejection entries
  are better AND want a wider target).

## Full-ledger validation — PASSES (the bar the prior B-sizing failed)

`scripts/continuous_v2_bybit_upperwick_fullledger.py` (2026-06-20): built a strictly-causal
per-symbol expanding-z upper_wick multiplier (932 keys, mult mean 1.0045, range [0.5,1.5]),
ran the 3 fade components through the REAL engine via `size_mult_lookup`, then
`build_full_ledger` (frozen rebalance + 2f hedge). Bybit.

| arm | MAR | max_dd | total |
|-----|----:|-------:|------:|
| control | 6.387 | −0.0130 | 0.2599 |
| **upper_wick sizing** | **6.497** | −0.0129 | 0.2618 |
| hash null | 4.879 | −0.0168 | 0.2565 |

- **MAR Δ vs control +0.110**, **MAR Δ vs hash +1.618** → `passes: true`.
- The hash permutation (same multiplier distribution, alignment destroyed) HURTS MAR
  (6.39→4.88), so the upper_wick→trade ALIGNMENT carries real information — not a
  distribution artifact. This is the test the prior both-venue B1 conviction sizing FAILED.

### Honest verdict — REAL and full-ledger-confirmed, but MODEST

upper_wick entry sizing is the FIRST mechanism in the entire continuous-v2 next-level
program to pass the full-ledger + hash bar: it improves Bybit MAR (+0.11, 6.39→6.50),
nudges total return up (+0.7%), leaves drawdown flat, and decisively beats its hash
(+1.62). The uplift is SMALL at the portfolio level (the 2f hedge + rebalance dominate the
hedged book's MAR; a within-book sizing tilt moves it only a little) — the equal-weight
proxy's +3.0 was the un-hedged per-trade view. So: a real, mechanism-backed, OOS,
hash-surviving, full-ledger-confirmed Bybit entry edge of modest size. The upper_wick-
conditional TP (+0.0014 OOS, beats hash) is a coherent companion.

**Status: an operator-gated Bybit-only forward-shadow CANDIDATE** (entry sizing tilt by
upper_wick) — the strongest entry result of the program. NOT a frozen-object change, NOT
real-money evidence; the next bar is a no-order forward shadow under a separate operator
receipt.

## No real-money / promotion claim

`REAL_MONEY` stays false. Bybit-only, operator-gated; full-ledger pass is in-sample working
-dataset evidence, not the forward-demo arbiter.
