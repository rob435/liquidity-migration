# Pre-registration: S1 Stage-A — staged "sniper" entries on the uptrend book

**Date:** 2026-06-09 (registered BEFORE the run). **Label:** `exploratory` Stage-A
(ledger-level; engine-grade intrabar build follows only on a PASS).
**Program:** downtrend/sniper S1 (program plan folded into
`docs/research_summary.md` on 2026-06-10; operator's idea: stops systematically sold
the squeeze wick → enter INTO the wick instead).

## Mechanism

Each official trade is replayed as two tranches: tranche A = fraction `a` of the
original notional at the official entry/exit (scaled official outcome); tranche B =
`1-a` as a LIMIT order at `entry_price x (1+x)` (deeper into the squeeze). B fills iff
the trade's recorded `mae >= x` (the engine's max-adverse-excursion says the wick
touched the level). B's exit = the ORIGINAL exit ts/price — deliberately CONSERVATIVE
(B's own take-profit could only improve it; omitting it cannot overstate). B pays the
same per-notional taker cost and FULL per-notional funding as A (over-charges B's
shorter hold — conservative). Unfilled B = idle (no fallback fill).

## Declared grid (no additions without amendment)

x ∈ {3%, 5%, 8%} x a ∈ {0.5, 0.7}; sensitivities on the best cell: a=0.3; maker-entry
B (fee+spread=0 on B's entry side); strict-wick fill (requires mae >= x + 0.2%);
per-component and per-year attribution.

## Evaluation

Per-trade net deltas are added to the component `raw_by_day` on the trade's exit day
(lumping approximation documented — applies to the DELTA only), components recombined
at frozen weights, rebalanced at the deployable anchor (w90/max4/ddh-0.04) WITH the
BTC hedge — i.e., scored directly against the deployable's headline (bybit MAR 5.52 /
binance 5.64, pooled 5.58).

## A-priori bars

- Stage-A PASS (gates the engine build): some declared cell with pooled hedged-max4
  MAR delta >= +0.5, positive MAR delta on BOTH venues, B fill-rate >= 15% (not a
  handful of trades), x-neighborhood consistent (no single-cell cliff), and DD not
  worse by more than 1pp on either venue.
- Adverse-selection check (reported): mean net of B-tranche fills vs the same trades'
  A-tranche — B must beat A on the SAME trades (the cushion must beat the selection).
- FAIL → record; the staged-entry idea dies at Stage-A and the MAR target must come
  from elsewhere.

## Artifacts

`~/SHARED_DATA/sniper_entries_2026-06-09/` — per-cell table + report JSON.
Script: `scripts/sniper_entries_stage_a.py`.

## Verdict — declared grid (filled in after the run, same day)

**Substitutive staging FAILS the bar** (best pooled hedged-max4 ΔMAR +0.15 at x=8%/a=0.7;
everything else negative). **But the pre-registered adverse-selection check is
strongly POSITIVE**: B-tranche fills net +224/+272/+333 bps (bybit, x=3/5/8%) and
+144/+187/+234 bps (binance) per unit notional, on trades where the A tranche nets
−67..−541 bps; fill rates 38–68%. The snipe leg has real per-trade alpha; the failure
mode is the FORGONE base size on never-wicking trades (the best immediate faders).
Maker/strict sensitivities don't change the sign.

## AMENDMENT 1 (dated 2026-06-09, registered BEFORE the amendment run)

Motivated by the declared diagnostic above (not by grid mining): test **ADDITIVE**
staging — keep the full base trade (a=1.0) and ADD tranche B = b x w as a limit at
entry*(1+x), same conservative fill/exit/cost/funding rules. Declared cells:
b ∈ {0.25, 0.5} x x ∈ {5%, 8%}. Risk note: this raises gross exposure specifically on
wicked (squeezing) names; the rebalance/DD layer absorbs it and the bar polices it.

Amendment bars (unchanged in spirit): pooled hedged-max4 ΔMAR ≥ +0.5 with both venues
positive ΔMAR; DD not worse by >1pp either venue; x/b-neighbor consistency; fill-rate
≥ 15%. FAIL → the sniper idea is closed at Stage-A in both forms.

**Amendment 1 result (same day): PASS at b=0.25** — x5: ΔMAR +1.11/+0.78 (pooled
+0.95); x8: +1.09/+0.89 (pooled +0.99); DD within 0.31pp; fills 38–55%. b=0.5 FAILS
(binance MAR −0.57 at x5; DD −1.34/−1.09pp — over-concentrates squeeze risk). The
b=0.25 row is x-stable.

## AMENDMENT 2 (dated 2026-06-09, registered BEFORE the run)

(a) Robustness battery on the two passed cells: 2x costs (component ledgers via the
scout cost-multiplier AND B's per-notional cost doubled), strict-wick fills
(mae ≤ −(x+0.2%)), per-year Sharpe split, funding-off attribution. Cells must keep
pooled ΔMAR ≥ +0.5 with both venues positive under 2x cost and strict fills.
(b) Declared construction cells: LADDER (b=0.125 at x=5% + b=0.125 at x=8%; total add
0.25) and MAKER-entry realism on B (resting limit: entry side pays 1bp maker instead
of 8bps taker+spread → B cost_pn credited +7bps) applied to x5_b0.25, x8_b0.25, and
the ladder. Same bars. Final cell selection among passers: highest pooled ΔMAR under
2x cost; tiebreak = simplest.
FAIL of (a) → the Amendment-1 pass is fragile; record and stop the arc.

**Amendment 2 result (same day): `x8_b25` SELECTED.** Full battery at 1x: x8_b25
ΔMAR +1.09/+0.89, ΔDD −0.16/−0.19pp, 2023-24 Sharpe +0.15/+0.26, strict-wick haircut
~0.1, maker adds ~+0.05. **2x-cost discriminates:** x8_b25 pooled ΔMAR +0.66 (PASS);
x5_b25 +0.25 (fail — binance +0.03); ladder +0.48 (marginal fail). Selection rule →
x8_b25 (taker-conservative). **New Stage-A headline: bybit MAR 6.61 / binance 6.53,
pooled 6.57 = +17.7% vs the hedged baseline 5.58.** Known conservative gaps for the
bar-accurate stage: B has no own TP and pays full-trade funding.

## AMENDMENT 3 (dated 2026-06-09, registered BEFORE the run)

Bar-accurate B-leg simulation (engine-equivalent for an ADD-ON leg; the official A
book stays the bit-exact ledger): B fills at touch from raw 1h bars (high ≥ level;
strict +0.2% variant), B exits at ITS OWN take-profit (level = B_entry x 0.9, fill at
level when bar low ≤ level, conservative same-bar rule: if both TP and further wick in
one bar, assume NO TP that bar... TP only counted from the NEXT bar after fill),
else at the original trade exit ts/price; funding pro-rata from actual funding events
in (fill_ts, exit_ts]; costs per tranche notional (taker primary, maker reported).
Cells: x8_b25 (primary), x5_b25 + ladder (reported). Bars: same as Amendment 2 (incl.
2x cost) + explicit assessment vs the +30% goal (pooled MAR ≥ 7.25). The Stage-A
mae-based result is the floor; if the bar-accurate result comes out LOWER than the
floor on either venue, stop and reconcile before any claim.

## Amendment 3 verdict + reconciliation (same day) — FINAL for this arc

The floor check TRIPPED (bar-accurate below the mae-based numbers) and the
reconciliation decomposes it: (1) Stage-A's "full-trade funding" shortcut OVERSTATED
B's funding credit (shorts receive positive funding in this regime; B's shorter
holds collect less — pro-rata is correct); (2) B's own TP truncates winners on bybit
(no-TP +1.17 vs own-TP +0.82) but protects on binance (own-TP +0.62 vs no-TP +0.09)
— a venue-flipping design effect we do NOT cherry-pick across. The declared
Amendment-3 primary (own-TP, x8_b25, taker, bar-verified fills, pro-rata funding):

| | 1x cost dMAR | 2x cost dMAR |
|---|---|---|
| bybit | +0.82 (MAR 6.34, DD -5.55, fill 39.9%) | +0.57 |
| binance | +0.62 (MAR 6.26, DD -4.72, fill 37.9%) | **+0.18** |
| pooled | **+0.72** -> book 5.58 -> 6.30 (+13%) | **+0.375 < +0.5 bar -> FAIL** |

**Verdict: Tier-1 lead, NOT a Stage-A pass.** The 1x improvement is real, both-venue,
DD-safe, fill-rich, with B-leg per-fill alpha ~+2-3%; but the pre-registered 2x-cost
robustness leg fails (binance margin too thin), so per the program rules this is
descriptive, not bankable. No further variant-mining: the declared menu is exhausted.
Honest paths to revive it: (a) engine-grade build where B is treated as the resting
LIMIT it actually is (maker economics — the declared maker variant adds ~+0.05 MAR at
1x and the 2x-taker stress arguably double-penalizes a passive order), with maker/queue
realism CALIBRATED from live demo fills (R4 ledger pull, operator-gated); (b) forward
demo evidence. Until then the deployable headline stays the hedged uptrend core
(5.52/5.64). Leverage-band table (reported, NOT a rescue): sniped max5 bybit MAR 7.19 /
binance 5.98 — leverage moves MAR but is not alpha and is not claimed as the
improvement. Artifacts: ~/SHARED_DATA/sniper_entries_2026-06-09/{report.json,
amendment1_additive.json, amendment2.json, amendment3_bar_accurate.json,
amendment3_reconcile_noTP.json}; scripts sniper_entries_stage_a.py,
sniper_entries_bar_accurate.py.

## AMENDMENT 4 (dated 2026-06-09, registered BEFORE the run) — engine-realism cost cell

One cell, no grid: the economically-correct passive-order model for B (own-TP x8_b25):
entry side pays MAKER fee 2bps with NO spread charge (it is a resting limit by
construction; exit side stays taker), fills require price trading THROUGH the level
(strict +0.2%) to answer queue risk conservatively. 2x stress doubles B's maker fee +
impact and the book's costs as before. Bars unchanged: pooled dMAR >= +0.5 at 1x AND
2x, both venues positive at both. PASS -> Stage-A banks (engine build gated on it).
FAIL -> arc closes as Tier-1 descriptive. This is the FINAL amendment of this arc.

**Amendment 4 result (same day): FAIL on the 2x leg** — strict+maker x8_b25: 1x
+0.82/+0.57 (pooled +0.70 PASS) but 2x +0.43/+0.17 (pooled +0.30 < +0.5). ARC CLOSED:
the sniper add-on is a real Tier-1 lead (+13% pooled MAR at modeled 1x costs, fills
~39%, DD-safe, both venues) that is NOT robust to doubled costs on binance and is
therefore not banked. Revival paths: maker/queue realism calibrated from live demo
fills (R4) or forward demo evidence. The deployable headline remains the hedged
uptrend core.

## AMENDMENT 5 (dated 2026-06-09, registered BEFORE seeing any fill data) — R4-calibrated banking test

Operator authorized the VPS key + confirmed the host key (pinned
SHA256:U8+LvEB...). Protocol, locked while blind to the data:

- **Calibration input:** the daily-demo order/fill ledgers (event demo sleeve — the
  shared execution path). Per filled taker order: realized per-side cost_bps =
  side x (fill_px - decision_reference_px)/reference x 1e4 + recorded fee_bps.
  Aggregate median/mean/p75 by side and notional bucket.
- **Validity gates:** >= 200 fills spanning >= 30 days with both sides present; demo
  fills calibrate the FIXED leg (fee+spread) only — the impact term stays modeled
  (demo cannot exhibit our impact; deploy sizes are small per the capacity receipt).
- **Banking re-test rule:** let C_obs = calibrated per-side fixed cost. Recompute the
  Amendment-3 primary (bar-accurate own-TP x8_b25) with fixed cost C_obs on BOTH the
  book and the B leg (honesty cuts both ways: if C_obs > the modeled 8 bps, the
  baseline gets MORE expensive too), and the stress leg at 2 x C_obs (+ modeled
  impact x2). BANK iff pooled dMAR >= +0.5 with both venues positive at the
  2x-calibrated stress, same DD/fill bars as Amendment 2. Insufficient fills ->
  no banking claim; record and wait for more forward evidence.

**Addendum 2026-06-10:** the box was fully rebuilt on 2026-06-09 after Amendment 5
was written; the host key rotated (current workflow default pin:
`SHA256:TJRbvgB8nfhwmNDv4hM3jDkPXnRv6BGLQ3cPst2PfE4`), so the `U8+LvEB` pin above
is the pre-rebuild key. Calibration source is amended to the rebuilt continuous
demo sleeve, which uses the same venue/account shared execution path; pre-rebuild
ledgers were lost, so the >=200-fills / >=30-days validity clock starts on
2026-06-09 from continuous-sleeve fills.

## AMENDMENT 6 (2026-06-10) — OPERATOR ADOPTION DECISION: Tier-2 demo candidate

The operator directed taking the improvement. Re-judged against the BINDING STATE.md
Tier-2 bar (the Amendment-2/3 "+0.5 pooled at 2x cost" was this program's own stricter
overlay, not the house rule): positive return both venues YES; pooled dMAR +0.72 >
+0.1 YES (and still +0.375 > +0.1 at 2x cost); neither venue < -0.5 YES; trade counts
YES (fills ~38-40% of ~3k trades). Under the house framework the 2x-cost stress is a
fragility diagnostic — REPORTED, NOT BLOCKING for demo candidacy — and it remains
reported above unchanged. **VERDICT: x8_b25 additive snipe = Tier-2 DEMO CANDIDATE by
operator decision; proceeds to live demo wiring with the ensemble+hedge executor.**
Unchanged and non-negotiable: Tier-3 (real money) stays strict; the 2x-cost margin
question gets settled by the demo box's observed-fill calibration (Amendment 5), which
is accruing as of 2026-06-09.
