# Pre-registration: E2 — BTC-trend regime-response family (3 pre-named variants)

**Date:** 2026-06-12
**Author:** operator + Claude (round-4 session)
**Stage:** proposed
**Plan:** folded into [research_summary.md](../research_summary.md); git history
is the archive for the old 2026-06-12 planning stub.

## What's changing

The binary BTC-30d-uptrend entry gate is compared against TWO pre-named
bounded alternatives (thresholds fixed up front):

- **V0** baseline: on iff `trend > 0` (current live gate).
- **V1** euphoria cap: on iff `0 < trend ≤ +0.20`.
- **V2** soft 3-state: `> +0.20` off; `0 < trend ≤ +0.20` full size;
  `≤ 0` quarter-size, top-composite-quintile entries only.

## Hypothesis

2026-06-12 exploratory bucket study (F5 in the plan): the fade book's mean is
NEGATIVE in the euphoria bucket the live gate currently trades (>+20%:
−136bps/24h, 29 episodes) and positive-but-clustered in deep crashes; the
response is non-monotone, so only a bounded family (not a score/curve) is
testable. Catastrophe days are uniform across buckets — this is a MEAN
question; disaster control stays with stops/caps. Funding (unmodeled in the
exploratory) pushes against both tails, so the registered run models funding.

## Predicted direction + magnitude

- V1: small pooled MAR improvement vs V0 (euphoria removal), trade count −10%
  to −20%.
- V2: MAR ≈ V0 ± noise with higher trade count; most likely casualty.
- Falsifier: V1 and V2 both miss the Tier-2 bar at Stage 1 — the binary gate
  stands.

## Roots that will be touched

- [x] bybit_full_pit (Stage-1 family run, funding ON — mandatory)
- [x] binance_full_pit (same; funding-missing label where the root lacks it)
- [x] forward demo/paper (Stage-2 shadow incl. pre-gate candidate evaluation;
      zero order impact)

## Decision rule (set before the run)

- **Sequencing:** Stage 1 may not start before E1's Stage-1 verdict is filed.
- **Stage 1 (decisive, Tier-2 bar vs V0):** full engine, all three variants,
  both venues, funding ON (mandatory), + 2x-cost arm. A variant WINS iff
  positive total return both venues, pooled MAR-Δ > +0.1 vs V0, neither venue
  MAR-Δ < −0.5, survives 2x cost. Episode counts (~29 euphoria / 24
  deep-crash, clustered) are REPORTED as the fragility diagnostic — disclosed,
  never used to rescue. Both variants miss: V0 stands, NULL receipt filed.
- **Stage 2 (adoption):** the winning variant (higher pooled MAR-Δ if both
  pass; no second look, no blending) is proposed to the operator as a
  demo-profile change; forward demo/paper accrues the live verdict; Tier-3
  stays forward-only. No new variants, no threshold adjustment, ever — a
  different idea requires a new pre-registration.

## Run command

```bash
# Stage 1: engine family run via the alpha_sweep dispatcher (cell spec recorded in
# the run commit; funding ON; both venues). Stage 2: shadow build item
# (pre-gate candidate evaluation, dynexit-shadow pattern).
```

## Post-run results

**Stage 1 ran 2026-06-12** (`scripts/e2_regime_family.py`; artifacts
`~/SHARED_DATA/e2_regime_family_2026-06-12/report.json`). Engine support for
V1/V2 added same-day (`uptrend_capped` + `soft3` gate modes, 2 tests,
existing paths byte-identical). V0 cells = the parity-verified rebuilt
component ledgers (p3 858/857 bybit, 722 exact binance); V1/V2 = the same 4
cells re-run per variant per venue; combine on frozen winner weights through
w90/tv0.045/max4/ddh-0.04; funding ON (bybit modeled / binance partial —
disclosed); 2x-cost arm via the scout cost-multiplier convention.

| variant/venue | base ret | base MAR | base DD | 2x ret | 2x MAR |
|---|---:|---:|---:|---:|---:|
| V0 bybit | +69.9% | 4.18 | −5.3% | +47.1% | 2.44 |
| V0 binance | +57.2% | 4.46 | −4.2% | +43.5% | 3.20 |
| V1 bybit | +50.6% | 2.48 | −6.6% | +36.8% | 1.57 |
| V1 binance | +34.6% | 2.24 | −5.1% | +26.8% | 1.72 |
| V2 bybit | +43.8% | 1.85 | −7.6% | +35.2% | 1.55 |
| V2 binance | +32.3% | 1.76 | −6.0% | +18.9% | 1.03 |

MAR-deltas vs V0: V1 pooled **−1.96** base / −1.18 at 2x; V2 pooled
**−2.52** base / −1.53 at 2x. Both variants fail every Tier-2 condition
except positive returns. The receipt's own prediction (V1 = small MAR
improvement) is FALSIFIED — recorded as such.

## Verdict

**NULL — V0 (the live binary uptrend gate) stands; both pre-named variants
are rejected and the family is closed.** The exploratory F5 bucket table
(euphoria mean −136bps on the no-trigger population) did NOT survive
conversion through the real engine: removing >+20%-trend days costs ~20pp
return, near-halves MAR, and worsens drawdown on BOTH venues — the
trigger-selected book's euphoria trades are net contributors once TP/exit
mechanics, funding (shorts collect in euphoria), and the rebalance rule's
equity dynamics are modeled. Methodology note for the record: this is the
mirror image of E1 — a same-trades reweighting could be adjudicated at trade
level, but a gate change alters the trade POPULATION and therefore required
the full engine, and the engine reversed the naive diagnostic's sign. No new
variants, no threshold tuning, per the registration. Down/euphoria-regime
treatment remains: hedge + per-name stops/caps.
