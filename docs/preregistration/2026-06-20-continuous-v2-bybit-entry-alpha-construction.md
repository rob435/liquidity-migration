# Construction + Result: Bybit Entry-Alpha — Exhaustion Features (operator direction 2026-06-20)

> ## ⛔ RETRACTION (2026-06-20): the full-ledger "pass" was a duplicate-counting artifact
>
> The headline full-ledger result below (vol-gated upper_wick sizing, MAR 6.387→6.555,
> +0.168, "the program's first full-ledger pass") DOES NOT HOLD. The live-wiring parity
> reconcile found that `build_upperwick_lookup` counted each of the 3 ensemble components as a
> separate point in the per-symbol expanding history (they share `(symbol, signal_ts)` ~61% of
> the time), which inflated the tilt; and that the per-symbol-expanding-z is too sparse
> (~2.7 entries/symbol vs the 10-obs minimum) to tilt at all when corrected. Re-validated with
> one principled observation per decision: **MAR 6.384, −0.003 vs control, −0.005 vs hash,
> passes=FALSE.** The signal's IC is real (+0.146 on gross) but it is NOT a tradable edge via
> this sizing — consistent with the program's recurring "real but not tradable" theme. The
> override was NOT activated. Retraction receipt:
> `docs/preregistration/2026-06-20-operator-override-upperwick-entry-sizing.md`. The OOS
> screens below used a different (global-z) construction whose full-ledger validity is UNTESTED
> and must not be assumed. Read the rest of this receipt as the (now-falsified) original work.


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

## Sensitivity sweep (operator: "why not more sensitive? test a broader range")

`scripts/continuous_v2_bybit_sizing_sensitivity.py`, decoupled (k = tilt strength, clip =
cap), fine/broad, IS(early) vs OOS(late) MAR proxy.

(A) k sweep at FIXED clip 3.0 — OOS MAR Δ vs control: k0.10 +0.97, k0.25 +2.52,
**k0.40 +4.20 (peak)**, k0.50 +4.07, k0.75 +3.61, k1.0 +3.11, k1.5 +2.78, k2.0 +2.22,
k2.5 +0.97, **k3.0 −0.40**. More sensitivity LOSES — OOS MAR peaks at k≈0.4–0.5 and goes
negative by k=3.0; the `%clipped` rises with k (0%→31%), i.e. cranking k just pins trades
at the cap (stealth admission), which degrades MAR. Classic weak-signal (IC ~0.15) result.

(B) clip sweep at FIXED k=0.5 — OOS MAR Δ: clip1.25 +1.84, **clip1.5 +3.12 (the original)**,
clip2.0 +3.97, **clip3.0 +4.07**, clip5.0 +4.03, clip10 +4.02. The original [0.5,1.5] clip
was TOO TIGHT — binding on ~18% of trades and chopping the gentle tilt. Widening to ~[1/3,3]
lifts the OOS proxy +3.1→+4.1, a broad PLATEAU (k0.4–0.6 × clip2–10 all ≈ +4.0; robust, not
a spike).

**Proxy refinement found, then FALSIFIED at full ledger:** the proxy said a wider clip (~3)
beats the original 1.5 (+4.1 vs +3.1). The full-ledger re-confirm (clip 3, k 0.5) REJECTS it:

| setting | upperwick MAR | Δ control | Δ hash | passes |
|---------|--------------:|----------:|-------:|:------:|
| clip 1.5 (original) | 6.497 | **+0.11** | +1.62 | YES |
| clip 3.0 (proxy "refinement") | 6.383 | **−0.003** | +2.88 | NO |

The MAR proxy (equal-weight per-trade, no hedge/rebalance) MISLED: a wider clip's bigger
tilts (up to 3x) concentrate weight in a way that interacts badly with the daily rebalance +
2f hedge, which the proxy cannot see. **Keep the original clip 1.5** (the validated setting).
Both still beat hash decisively, so the SIGNAL is real either way; only the clip-WIDTH tweak
fails. Methodology win: the proxy is fine for ranking signals but NOT for portfolio-
construction params (clip width) — full-ledger is the arbiter, and it kept the conventional
gentle clip. More sensitivity (higher k) is closed too.

## Recency-weighting test (operator: "should it be an EMA favouring recent wicks?")

`scripts/continuous_v2_bybit_wick_recency.py` — IC vs gross for EMA half-lives 5..120m,
linear-recent, last-15/30, and an OLDEST-weighted falsifier. Bybit, train/test stable.

| variant | IC gross | IC MAE |
|---------|---------:|-------:|
| **uw_mean (simple)** | **+0.146** | −0.005 |
| ema h120 (≈mean) | +0.142 | −0.013 |
| ema OLDEST-weighted (falsifier) | +0.136 | +0.020 |
| ema h60 | +0.130 | −0.022 |
| ema h30 | +0.105 | −0.034 |
| ema h15 | +0.066 | −0.054 |
| last15 / ema h5 | +0.024 / +0.023 (unstable) | −0.07 |

**Recency-weighting monotonically HURTS** — the more recent the weighting, the weaker the
IC, down to unstable near-zero for the shortest. The simple full-window mean is best. The
OLDEST-weighted falsifier (+0.136) BEATS every recency-weighted variant, so the signal if
anything leans to EARLIER exhaustion — the opposite of recency. Recent windows are also
noisier (sign-flip OOS) and couple more to MAE. Interpretation: the predictive content is
PERSISTENT/broad rejection across the 120m (sustained exhaustion), not the freshest wick;
also the +1h entry delay puts the signal event well before the recent minutes. **Keep the
simple mean** (the form already full-ledger-validated). Recency EMA: closed.

## Taker-flow exhaustion (operator: "go after order flow")

`scripts/continuous_v2_bybit_takerflow.py` + `...flow_combo.py`, from the bybit_full_pit
`taker_flow_5m` tape (taker_buy/sell_quote, n_buy/n_sell; 100% causal coverage of the
V2_CONTROL windows). Theory: best fades are buyer-EXHAUSTION pops.

Feature IC vs gross (vs upper_wick +0.146): **absorption +0.163** (price rose WITHOUT
aggressive net buying = hollow pop → better fade) is the best, ~orthogonal to upper_wick
(corr +0.10). `buy_ratio` −0.07 and `cvd_slope` −0.07 (real aggressive buying = momentum =
WORSE fade — signs cohere). So order flow found a HIGHER-IC signal than the candle shape.

BUT the decisive test (sizing) flips it — higher IC ≠ better signal:

| signal | IC gross | IC MAE | OOS sizing MAR Δ |
|--------|---------:|-------:|-----------------:|
| upper_wick (clean) | +0.146 | −0.005 | **+4.07** |
| absorption (raw) | +0.163 | −0.162 | **−5.23** |
| uw + absorption | +0.215 | −0.116 | −2.04 |
| absorption residualized on run_up+rv | +0.096 | −0.058 | −0.86 |
| uw + resid-absorption | +0.173 | −0.041 | +1.25 |

- **Absorption is RISK-COUPLED** (IC_mae −0.16): it predicts pops that revert further but
  also drop further first, so sizing up on it DESTROYS MAR (−5.23) despite the higher IC.
- **Residualizing** absorption on the risk features (run_up, rv; fit early, apply late)
  cleans it (IC_mae → −0.058, MAR → −0.86) — but the cleaned residual is too WEAK (+0.096)
  to beat upper_wick; the combo (+1.25) is still worse than upper_wick alone (+4.07).

**Verdict — order flow confirms the mechanism but does not improve the edge.** Absorption
gives order-flow evidence for the exhaustion story (hollow pops revert), but its predictive
power is mostly RISK PREMIUM, not clean alpha; stripped of that it cannot beat the
candle-shape signal. **upper_wick alone stays the best deployable entry signal.** Flow
angle closed.

## Meta: three knobs tested, all confirm upper_wick (simple mean, gentle clean sizing)

Sensitivity (k), recency (EMA), and order flow were each tested rigorously (OOS + hash +
falsifiers). All three came back showing the simple-mean upper_wick with a gentle clean
sizing tilt is already the right form — the signal is real but intrinsically modest, and
incremental feature engineering on this book is clean-signal-capped. The edge is what it is.

## Do inverse-vol and upper_wick sizings combine? + vol-gated refinement (operator)

The validated full-ledger run already applies upper_wick MULTIPLICATIVELY on top of
inverse-vol (nw = invvol_weight x wick_mult); the +0.11 is the marginal gain over inverse-
vol alone. Orthogonality (`continuous_v2_bybit_sizing_orthogonality.py`): corr(upper_wick,
inverse-vol weight) = -0.19 (mild opposition), corr(uw, rv) = +0.10; upper_wick IC vs gross
WITHIN invvol-weight terciles = +0.04 (high-vol) / +0.20 (mid) / +0.12 (low-vol) -> the
alpha survives controlling for vol = largely ORTHOGONAL (risk axis vs quality axis), clean
stacking. Nuance: upper_wick is BLIND on high-vol names where inverse-vol downsizes -> the
ungated tilt wastes effort / mildly opposes there.

Vol-gated refinement, OOS screen then FULL-LEDGER confirm:
- OOS screen (`continuous_v2_bybit_volgate_screen.py`): HARD gating hurts (gate-top33 proxy
  MAR +1.77 vs ungated +3.12 — crude, discards the weak high-vol signal); SMOOTH
  vol-attenuation (taper tilt by 1 - causal vol percentile) edges ungated (+3.38 vs +3.12).
- Full-ledger (strictly-causal per-symbol expanding rv percentile, --vol-attenuate):

  | arm | MAR | total | max_dd |
  |-----|----:|------:|-------:|
  | control (invvol only) | 6.387 | 0.2599 | -0.0130 |
  | upper_wick ungated | 6.497 | 0.2618 | -0.0129 |
  | **upper_wick VOL-SMOOTH** | **6.555** | 0.2600 | -0.0127 |

  vol-smooth: **+0.168 vs control, +0.058 vs ungated, +1.62 vs hash, passes=True**.

**Verdict — YES, they combine, and vol-gating the wick tilt genuinely improves it.** Smooth
vol-attenuation lifts MAR +0.058 over the ungated tilt (and +0.168 over inverse-vol alone),
confirmed at full-ledger + hash. Mechanism: it gives up a sliver of return (0.2600 vs 0.2618)
but cuts drawdown more (-1.27% vs -1.29%) — not tilting on the high-vol blind spot avoids
bad concentration there. The two sizings are complementary: inverse-vol = RISK sizing,
upper_wick (vol-gated) = QUALITY sizing on the names where quality is measurable.

**Methodology contrast (important):** the clip refinement's proxy gain was REJECTED by the
full ledger; this vol-gate refinement's proxy gain was CONFIRMED. The proxy is trustworthy
for SIGNAL/SELECTION changes (which trades get weight) but NOT for CONCENTRATION/SHAPE params
(how hard to tilt / clip). Vol-gating is the former; the clip was the latter.

**Caveats:** the absolute gain is small (+0.058 over ungated; the 2f hedge dominates hedged
MAR). Mild design selection (smooth chosen as best of 4 OOS gate designs) — but theory-led
(hard gating is crude) and full-ledger-independent-confirmed (smooth + smooth_sqrt both beat
ungated OOS). The validated Bybit entry lead is now **vol-gated upper_wick sizing** (+0.168).

## No real-money / promotion claim

`REAL_MONEY` stays false. Bybit-only, operator-gated; full-ledger pass is in-sample working
-dataset evidence, not the forward-demo arbiter.
