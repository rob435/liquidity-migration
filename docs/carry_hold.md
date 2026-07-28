# Carry-hold — strategy document

The owner-selected lead strategy from the 2026-07-26 financed-longs program
(`docs/research_2026-07-26_financed_longs.md`). This document is the single
reference for what carry-hold is, why it works, what it has been tested
against, how it should be run, and what would kill it. Registered config:
`configs/lane2_carry_hold_v1.json` (commit `6584b00` + correction `7f2e0a7`);
executable: `liquidity_migration/financed_longs.py`.

Status: **Lane-2 registered, accruing a forward record since the registration
commit. Not deployed. No runtime, no venue access, no real-money implication.**

## 0. 2026-07-28 correction — read this before any number below

The registration-era scorer **charged every 8h/4h/2h funding settlement
twice** (float-epsilon age bug; fixed 2026-07-28 with regression tests —
full statement in `docs/research_2026-07-26_financed_longs.md` §0). Trades,
entries, exits, price legs, and costs are unchanged; the funding P&L leg was
inflated. Corrected, on the benchmark window: **Sharpe 1.21 raw / 1.05 vt vs
the benchmark's 1.84** — carry-hold **no longer beats the deployed sleeve on
Sharpe** (return still beats; the owner goal was both). Corrected full-sample
t 2.31 (below the ≈3.4 multiple-testing bar); corrected attribution
**funding +7.2 units vs price −3.4** (2.1:1, not 3.4:1); corrected eras
(bp/day): 2021 +3.8 · 2022 +3.0 · 2023 +26.0 · 2024 +13.7 · 2025 +30.3 ·
2026 +32.5 — the 2021-22 bear-robustness claim is withdrawn. The mechanism
(crowded shorts pay longs) is real but roughly half the registered size.
Sections below preserve the registration-era text; funding-dependent numbers
in them are superseded by this section. Also relevant: Bybit shortened
funding intervals through 2025-26 (52% of 2025 settlements are 4h, 21% 1h) —
73–80% of this book's 2025-26 held name-days are on sub-8h names, so the
per-print −10/−3 bp thresholds mean different daily carry per symbol; a
successor should normalize thresholds to a daily-equivalent rate.

## 1. The trade in one paragraph

Hold long, with perpetual futures, the handful of liquid names whose funding
rate has gone deeply negative — the names crowded shorts are paying to press —
and stay in each name until its funding normalizes. Collect the funding three
times a day; ride whatever squeeze develops; cap every name at 10% of the book
and the whole book at 1× gross so cascades dilute the book instead of levering
it. That is the whole strategy. There is no forecast in it: the position *is*
the payment.

## 2. Mechanism and counterparty

Funding is the price of one side of a crowded perp market. When it prints
deeply negative, shorts are so crowded they pay longs ~3×/day to keep the
position on. The carry-hold book supplies the long side of that demand:

- **Who pays**: leveraged shorts (directional bears, hedgers, delta-hedged
  structures) who must hold through the settlement stamps.
- **Why it persists**: the payment is compensation for real risk — these names
  are usually falling, sometimes to zero (LUNA was entered by this rule in May
  2022 and is in the record). The premium survives *because* the risk is
  unpleasant; the 2026-07-25 program (§18.5–§19.5 of the anomaly research)
  found exactly this asymmetry: the unhedgeable version pays in 6/6 eras, the
  comfortable delta-neutral version was arbitraged out by 2022.
- **Measured attribution 2021-2026**: **+13.06 units of capital from funding
  received vs −3.86 from price** — a 3.4:1 carry payment, not a price anomaly.

## 3. The exact rule

Universe and cadence:

- Venue Bybit USDT perps; universe = **top-100 by trailing-24h quote
  turnover**, re-ranked at each decision bar; 168h of price history required.
- Decisions once per day at the 00:00 UTC hourly bar close.

State machine, per name (all inputs are the **last settled** funding rate —
a historical, already-paid print; no predicted rates):

- **Enter LONG** when settled funding < **−10 bp/8h**.
- **Stay** while settled funding < **−3 bp/8h** (hysteresis; funding noise
  between −3 and −10 does not churn the position).
- **Exit** at the first decision bar where funding has recovered to ≥ −3 bp.

Sizing and risk:

- **0.10 of capital per name**; **total gross capped at 1.0** — when more
  than ten names qualify (cascades), every weight scales down pro rata. The
  cap direction matters: the un-capped book trebles gross exactly during
  market-wide cascades, which is when correlation goes to one.
- Optional (registered) **15% annual vol target, 3× leverage cap**, scale
  computed from strictly-prior 30d realized vol, leverage-change turnover
  charged. Sizing controls drawdown *depth* only; see §6.
- Book state: typically ~3.8 names when active; in cash when nothing
  qualifies (28% of days over the full sample; deep-negative funding was rare
  in the 2021 bull, common after the 2024-25 funding inversion).

Costs and accounting, as scored:

- Fees: measured one-way turnover × **7.78 bp/side** (the measured demo taker
  fee; turnover averages 0.35 units/day ⇒ ≈2.7 bp/day).
- Funding settlement-exact; entries at the decision bar close
  (`execution_delay_ms=0` convention — see delay robustness in §5).

## 4. Performance vs the deployed benchmark

Benchmark: CONTINUOUS sl35 render, regenerated 2026-07-26 — Sharpe 1.84,
+15.85%, max DD −2.85% over 2023-03-13→2026-07-16. Full-calendar basis
everywhere (flat days = 0 in the denominator, identical to the benchmark's own
accounting).

*(Benchmark change point, 2026-07-26 later the same day: the deployed
CONTINUOUS shape was replaced at commit `1fe0e48` — revision
`active_single_fund0_tp12_sl35_v1`, same-window render Sharpe 1.45 / +11.06% /
max DD −1.84%. The table below stays as written against the pre-change
benchmark; forward comparisons grade against the new one.)*

| bench window, full calendar | carry-hold | benchmark |
| --- | ---: | ---: |
| Sharpe raw / vol-targeted | **2.57 / 2.41** | 1.84 |
| total return raw / vt | +21,943% / +342% | +15.85% |
| max DD (vt) | 23.7% | 2.85% |
| worst day (vt) | −5.7% | −0.70% |
| t-stat | 4.69 | — |
| one-way turnover | 0.35 units/day | — |

Full-sample eras (bp/day, active-day series): 2021 +25.4 · 2022 **+19.1** ·
2023 +54.3 · 2024 +32.9 · 2025 +77.6 · 2026 +71.5. Positive through the 2022
bear because the trigger *is* the compensation signal — the book sits out
grind-downs where shorts are not paying.

## 5. Validation battery (what was actually checked)

1. **Cross-venue replication**: identical construction on Binance funding and
   prices: +25.04 bp/day, t 2.73, effect ratio 0.50 — inside the house 2A band
   [0.5, 2.0]. First positive mechanism in this repository's program to
   survive replication. corr(bybit, binance books) = +0.85.
2. **Entry-delay stress**: +1h → t 4.19; +4h → t 4.82. The edge does not live
   at the print; there is no stale-price artifact.
3. **Placebo**: same active days, same gross, top-100 basket instead of the
   selected names: Sharpe 0.72 vs 2.76 (active-day basis). ~90% of the return
   is name selection, not market timing.
4. **Worst-day forensics**: the worst days are real cascade events (LUNA week
   2022-05, 2021-12-04, 2022-06 Celsius); no data artifacts. The gross cap is
   what bounded them.
5. **Parameter plateaus** (all cells reported in the research note): enter/exit
   {5/2, 10/3, 15/4, 20/5} → Sharpe 1.92–2.64; universe {50/100/300} →
   2.26–2.61; knife-filter variants change little. No spike-fitting.
6. **Slippage sensitivity**: +2 bp/side beyond the measured fee → bench Sharpe
   2.57 → 2.53.
7. **Multiple testing**: t 4.87 (full sample, full-calendar basis; 4.88 on the
   superseded active-days-only basis) against the ≈3.4 Bonferroni threshold for
   the ~63 mechanisms this program has tested.
8. **Funding-sign accounting** is covered by unit tests
   (`tests/test_financed_longs.py`): a long receives negative funding
   settlement-exactly; hysteresis state uses only past prints; the gross cap
   dilutes.

## 6. Risk profile — read this before sizing

- **Underwater behavior**: at 15% vol the longest underwater spell in the
  bench window is **204 days (2024-02-26 → 2024-09-17)**, 87% of days are
  below a prior peak, max DD 23.7%. For calibration: the deployed CONTINUOUS
  system's own longest spell is 215 days at its native scale — long spells
  are endemic to every book here; **sizing changes depth, never duration**.
  At half size (vt 7.5%) the same book is max DD 11.6%, +105% over the
  window, same Sharpe.
- **Concentration**: ~3.8 names. A single-name disaster costs up to its 10%
  cap ex-slippage. The book *will* hold names that go to zero (it held LUNA);
  the claim is that the funding collected across the book pays for them, as
  it has 2021-2026.
- **Regime dependence**: deployment frequency tracks the funding regime. In
  2021 the book was ~78% cash. If funding normalizes market-wide, the book
  goes quiet rather than long — returns flatten, they do not invert.
- **Market beta**: +0.30 correlation to BTC on active days (long-only book).
  corr with the deployed CONTINUOUS: **−0.08** — additive, not overlapping.
- **Capacity**: the deep-negative-funding subset of top-100 names at demo-account
  scale. Fees are measured at small notional; no impact model. This is not a
  large-book claim.

## 7. Implementation path (when/if promoted)

Not deployed today. The path, in order, per `docs/governance.md`:

1. **Forward record** accrues from commit `6584b00`; score one row per
   completed UTC day (`scripts/screen_financed_longs.py` reproduces; the
   scorer belongs in the research-refresh cadence).
2. **Runtime design** when the record earns it: a target producer that reads
   settled funding (the account owner already consumes funding for
   reconciliation), maintains the per-name state machine, and publishes
   targets through the normal account-kernel path — TP/SL declared like the
   sl35 pattern (a declared wide stop, e.g. −35%, so the 2% account fallback
   never becomes the exit; the funding-normalization exit is the strategy
   exit). Chase-then-cross entries; exits are unhurried by construction.
3. **Demo first**, paper in parallel; promotion needs the five-line note and
   a recorded change point. Real money remains a separate owner door.

## 8. Proposed kill criteria (pre-registered before the evidence arrives)

To be armed with the forward record, mirroring
`docs/preregistration/sleeve_kill_criteria_2026-07-20.md` discipline:

- **K1 (drawdown)**: vol-targeted forward drawdown > **30%** from the forward
  peak ⇒ demote to research.
- **K2 (dead run)**: 120 consecutive forward days with the book deployed ≥ 30%
  of days and cumulative net ≤ 0 ⇒ demote.
- **K3 (mechanism break)**: funding received minus price bleed (the §2
  attribution, recomputed on forward trades) turns negative over any 90-day
  deployed stretch ⇒ demote — the payment, not the price, is the thesis.
- **K4 (insufficient sample)**: fewer than 25 deployed days in 180 calendar
  days ⇒ no verdict either way; keep accruing, do not promote.

## 9. What this document does not claim

Lane-1 numbers selected this rule; only the forward record grades it. Fees are
measured, impact is not. The 2025-26 contribution rides the structural funding
inversion; a normalization shrinks the opportunity set. The Binance arm
replicates the mechanism but does not independently beat the deployed
benchmark's Sharpe on the full-calendar basis (1.66 vs 1.84). Correlation
+0.75 with `lane2_financed_leaders_v1` — the two harvest one macro-premium in
different phases and must not be presented as independent bets.
