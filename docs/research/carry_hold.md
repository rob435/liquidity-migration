# Carry-hold — strategy document

The owner-selected lead strategy from the 2026-07-26 financed-longs program
(`docs/research/archive/2026-07-26-financed-longs.md`). This document is the single
reference for what carry-hold is, why it works, what it has been tested
against, how it should be run, and what would kill it. Registered config:
`configs/lane2_carry_hold_v1.json` (commit `6584b00` + correction `7f2e0a7`);
executable: the rule in `liquidity_migration/rules/carry_hold.py`, scored by
`liquidity_migration/research/backtest/financed_longs.py`.

Status: **Lane-2 registered, accruing a forward record since the registration
commit.**
*(2026-07-28: the owner promoted `lane2_carry_hold_v3` to lead config of the
family — the reference for any §7 implementation work. Research-only status
unchanged; v1/v2 keep scoring. Trade diagnostics:
`reports/carry_hold_v3_trade_diagnostics_2026-07-28/`.)*
*(2026-07-31: **`lane2_carry_hold_v4` is registered and is the lead config**
— see §0.1. v1/v2/v3 all keep scoring and are unchanged; a regression test
pins that v4 moved nothing in them.)*
*(2026-08-03: **v4 is PROMOTED to the demo CARRY sleeve** by owner override —
producer profile `carry_hold_v4_live_v1`, promotion note and forward-record
caveat (0 scored days at promotion) in
`docs/research/strategy_program.md`, deploy receipt in `CHANGELOG.md`.)*
*(2026-08-19: **v5 and v6 are registered** — v5 adds the flow and whale size
halvings, v6 bends the depth ladder with a 1.5 exponent on top — and **v6 is
PROMOTED to both CARRY producers the same evening** by owner override:
profile `carry_hold_v6_live_v1`, 0 scored forward days at promotion, v4/v5
keep scoring as comparators. The whale leg makes the deployed book read one
non-Bybit input (Binance top-trader EODs, fail-open). Promotion note in
`docs/research/strategy_program.md`, deploy receipt in `CHANGELOG.md`.)*
*(2026-08-19 later that night: **v7 is DEPLOYED** — an execution-clock
version, NOT a new registered config: profile `carry_hold_v7_live_v1` trades
this file's v6 membership byte-identical and fires the early exit on the
venue's pre-settlement running rate inside the last 15 minutes. This
document's numbers are unaffected; the clock evidence lives in
`research_findings.md` §Settlement-instant timing.)*

## 0. Corrections and registrations — read this before any number below

**The 2026-07-28 funding double-count.** The registration-era scorer **charged
every 8h/4h/2h funding settlement twice** (float-epsilon age bug; fixed
2026-07-28 with regression tests — full statement in
`docs/research/archive/2026-07-26-financed-longs.md` §0). Trades, entries, exits, price
legs, and costs are unchanged; the funding P&L leg was inflated. Sections below
preserve the registration-era text; funding-dependent numbers in them are
superseded by this section, pair for pair:

| quantity (section where the old number still stands) | registration-era | corrected |
| --- | ---: | --- |
| benchmark-window Sharpe, raw / vol-targeted (§4) | 2.57 / 2.41 | **1.21 / 1.05** against the benchmark's 1.84 — carry-hold **no longer beats the deployed sleeve on Sharpe** (return still beats; the owner goal was both) |
| full-sample t (§5.7) | 4.87 | **2.31**, below the ≈3.4 multiple-testing bar then in force |
| attribution, funding vs price, units of capital (§2) | +13.06 vs −3.86, 3.4:1 | **+7.2 vs −3.4, 2.1:1** |
| eras 2021 · 2022 · 2023 · 2024 · 2025 · 2026, bp/day (§4) | +25.4 · +19.1 · +54.3 · +32.9 · +77.6 · +71.5 | **+3.8 · +3.0 · +26.0 · +13.7 · +30.3 · +32.5** — the 2021-22 bear-robustness claim is withdrawn |
| Binance replication (§5.1) | +25.04 bp/day, t 2.73, effect ratio 0.50 | **t 0.4 / Sharpe 0.18 — withdrawn.** The doubled funding leg *was* the replication. Single-venue (Bybit) evidence until shown otherwise |

The optional vol-target overlay (§3) hurts on corrected accounting — vt 1.05 <
raw 1.21 here, worse for v2 — so **raw is the primary basis**. The mechanism
(crowded shorts pay longs) is real but roughly half the registered
size. Bybit also shortened funding intervals — the mix went 100% 8h in 2021 to
**52% 4h / 21% 1h in 2025**, and 73–80% of this book's 2025-26 held name-days are
on sub-8h names, so the per-print −10/−3 bp thresholds mean different daily carry
per symbol. ~~A successor should normalize thresholds to a daily-equivalent
rate~~ — tested and refuted in the 2026-07-28 quant review: the variant
collapses, per-print acuteness is load-bearing.

**Registered by that review**
(`docs/research/archive/2026-07-28-carry-hold-quant-review.md`); v1 keeps scoring, and
the paired daily differential is the primary forward comparison:

- **`configs/lane2_carry_hold_v2.json`** — a sizing refinement: same state
  machine, each held name's weight scaled by `clip(|trailing 24h settled
  funding| / 120 bp-per-day, 0.25, 1.0)`, so bet size follows the premium being
  paid. Seen-data effect: same mean (17.0 vs 18.0 bp/day, paired t −0.4), max DD
  −60.0% → −48.6%, Sharpe 1.02 → 1.11, MAR 0.97 → 1.25, turnover −27%.
- **`configs/lane2_carry_hold_v3.json`** (same-day wave 2) — v2 plus a toxic-band
  filter (no entries, holds suspended, while the 3d return sits in [−30%, −5%) —
  the shorts-slowly-winning cohort), a 5%/day minimum-vol entry floor (pinned
  prices have no squeeze fuel), and a +30 bp/2d trail-recovery exit (squeeze
  over). Seen-data: Sharpe 1.38 / MAR 2.84 / DD −28.7% vs v2's 1.09 / 1.21 /
  −48.6%. Read its `honesty_notes` before quoting any number: the single-clock
  level rides midnight decision-hour luck (12-offset sweep 0.30–1.52; ensemble
  ~1.2), the daily frame carries a terminal-day look-ahead (~+0.13), and the
  owner's unconditional Sharpe-2 target was NOT reached — the supportable version
  is conditional (2.15–2.35 on the PIT deep-funding half of days).
- The funding-spread config (wave 3; **DELETED 2026-08-19, operator
  override**) — the same premium captured market-neutrally as a cross-venue
  funding spread (Sharpe 1.34 full / 1.61 bench, DD −16.7%, offset-stable,
  corr +0.09 to v3). The two-book funding-carry program (PIT vol-parity)
  measured bench-window Sharpe 2.34 (1.93–2.34 across clocks) / full-window
  1.55–1.87. Numbers kept as the record; the config and its scorer code are
  gone. Review §10.

### 0.1 2026-07-31 — v4 registered, and the program bar moved to 2.5

Two changes, one config (`configs/lane2_carry_hold_v4.json`), both on seen data
and therefore grading nothing yet.

**Crowding persistence, used as a size and not a screen** — the share of a
symbol's last 20 **settlements** that printed deeper than the 10 bp entry
threshold. Depth (v2's ladder) is how much the crowd is paying now, persistence
is whether this name pays habitually, and the two multiply. Counted in the
symbol's own settlement sequence, never on a clock: given the shifting interval
mix above, an hours-based version reports cadence, and the confound has an era
gradient. The isolated deep print is the only losing cohort in v3's book (−16.7
bp/name-day over a third of held name-days); every bucket above the 10% cut earns
+99 to +135.

**The toxic band's high edge, −5% → 0%.** The [−5%, 0) cohort earns −34.4
bp/name-day and v3 keeps it. On its own this change measures **t 1.12 — it does
not clear the bar even at 2.5**, and it is in v4 at the owner's direction. Its
contribution is on its own line in the config so a later reader can withdraw it
without touching the sizing result.

**Read the numbers in this order.** v4 and v3 do not span the same record
(1,756 days vs 1,894 — v4 does not trade early-2021). On the shared spine:

| | mean gross | bp/day | Sharpe | max DD | MAR | turnover |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| v3 | 0.1362 | +21.12 | 1.41 | −28.69% | 3.08 | 0.156 |
| v4 | 0.0948 | +22.19 | **1.64** | **−24.46%** | **4.14** | **0.119** |
| v4 at v3's capital | 0.1362 | +31.88 | 1.64 | −33.52% | 4.67 | — |

MAR is compounded annualised return ÷ max drawdown — the convention v3 registered
and `scripts/research/equity_curves.sh` renders. Simple annualisation of the daily
mean gives 2.69 / 3.31 / 3.47 for the same three rows; do not mix them.

- **Sharpe 1.41 → 1.64 is scale-free** and holds either way — cite it first. MAR
  is *not* scale-free: 3.08 → 4.14 at v4's own capital, 3.08 → 4.67 at v3's.
  Always say which capital a MAR is on.
- **At its own capital v4 is not a return improvement**: +1.07 bp/day, t 0.47 —
  do not cite it as one. **The claim is capital efficiency**: run at v3's average
  capital the paired differential is **+10.76 bp/day, t 3.23**, and that is the
  registered forward experiment. **And that leverage costs drawdown** — at v3's
  capital v4's worst dip is 33.5%, *worse* than v3's 28.7%; at its own capital it
  is better, 24.5%. You get one or the other, and which one is a leverage choice.
- Benchmark window: v4 Sharpe **1.88**, the first carry-hold render above the
  retired CONTINUOUS research benchmark of 1.84 — on seen data, so it grades
  nothing, and that benchmark is a retired research render, not the shipped book
  (1.45). On that window v4 is MAR 6.11 against v3's 4.85.
- **The curve is a 2025-26 story**, rendered through the standard chart
  (`reports/equity_curves/research/lane2_carry_hold_v4/`): **76.2% of the log
  growth is 2025-26** — 2.21x on 2025-01-01 after three years, finishing at
  28.83x. By year: 2021 −0.6%, 2022 +23.5%, 2023 +48.7%, 2024 +24.1%, 2025
  +260.5%, 2026 +266.1% (207 days). v3 has the same shape (76.7%, 2.13x) — the
  mechanism's regime dependence, not something v4 added.
- **What it costs**: v4 is more concentrated than v3 — 2,050 held name-days over
  944 active days at 2.17 names per active day, against 3,314 / 1,211 / 2.74 —
  and out of the market 46% of days against v3's 31%. The measured drawdown is
  lower anyway, but concentration risk taken on the same history that selected
  the rule is not evidence about the next drawdown.
- **Placebos, which are why this was registered at all**: sizing *up* the
  isolated prints, −14.44 bp/day (t −2.73); handing the identical distribution of
  position sizes to the wrong names, −15.26 (t −2.71) — the load-bearing control,
  because it holds size distribution and gross constant. Null persistence fails
  open and never fires on this book (0 of 3,314 held name-days), so it is not a
  listing-age screen.

**The program bar is now t ≥ 2.5** (`docs/research/governance.md` §2, owner decision
2026-07-31), replacing the family-wise ≈3.25/3.58. It is prospective: verdicts
recorded before that date stand as written. The bar no longer controls
family-wise error, so a plateau and a failed placebo now carry the weight the
threshold used to — v4 has both (16 of 16 shape cells positive, t 1.87–2.77).

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
7. **Multiple testing**: registration-era t 4.87 (full sample, full-calendar
   basis; 4.88 on the superseded active-days-only basis) against the ≈3.4
   Bonferroni threshold then in force. **Both halves are superseded**: §0
   corrects the t to 2.31, and 2026-07-31 replaced the threshold with a fixed
   t ≥ 2.5 (`docs/research/governance.md` §2). v1 clears the current bar on the corrected
   number; it did not clear the one in force when it was registered.
8. **Funding-sign accounting** is covered by unit tests
   (`tests/research/backtest/test_financed_longs.py`): a long receives negative funding
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
  - **2026-07-30, first measured impact evidence** (n=3 live entries, against the
    exact decision books captured at entry). A single
    **$25k** clip already exhausts most of the *visible top-50 levels*:
    `LAUSDT` 79% covered, `ESPUSDT` 74%, `VANRYUSDT` 100% (13 levels).
    Book-walk VWAP for the full clip was **+47.4 / +45.7 / +16.1 bp** — roughly
    **6× the 7.78 bp/side the scorer charges**. Realised execution was much
    better than that (effective spread **−29.2 / +3.2 / −2.3 bp**) because the
    producer chunks (5 / 2 / 2 clips over 0.4–2.4 s) and the book refills
    between clips, beating the visible walk by 17–62 bp. Read it as: the fee
    model is fine, the *impact* model is still absent, and patience is what is
    currently paying for its absence. Nothing here supports scaling this book
    an order of magnitude without re-measuring.
  - Observed taker fee is **5.50 bp/side, exactly**, on all 346 live orders —
    the §3 assumption of 7.78 bp/side is conservative by 2.28 bp/side.

## 7. Implementation path — BUILT AND DEPLOYED 2026-07-29 (owner override)

The owner ordered on 2026-07-28/29: retire CONTINUOUS from the forward routes
and deploy this strategy in its place. The runtime now exists:

1. **Sleeve**: `carry` — target producer `liquidity_migration/strategy/carry_demo.py`
   (+ `carry_demo_daemon.py`), demo unit (a paper twin existed until the
   2026-08-03 paper retirement), publishing absolute
   component targets as a target book the engine reads
   ([`rules/engine_targets.py`](../../liquidity_migration/rules/engine_targets.py);
   at deploy time this was the account-kernel inbox, which went with the
   Python order path on 2026-08-14). The deployed
   decision logic is NOT a reimplementation: the producer calls the exact
   registered-scorer functions (`rules/carry_hold.py:carry_hold_weights`) on the
   live frame (`rules/carry_hold.py:prepare_decision`) over a stateless 90-day
   replay window (longest-ever state spell: 19 days).
2. **Decision cadence**: once per UTC day at 00:00 close, computed from
   ~00:20 (kline availability lag, same offset as the rmom timer). Diff-based
   idempotent publication; quiet cycles between decisions. Data: 1h klines
   stream in over WebSocket into an in-memory store that serves the cycle's
   whole window, with REST covering gaps and restarts (since 2026-08-03; for
   the first hours a window off-by-one meant the store streamed without
   serving a single cycle — fixed and verified the same day). Settled funding
   history and the hourly sweep stay REST — the venue has no stream for it.
   Same bars, same close keys; only the transport changed.
3. **Protection**: declared `stop_loss_pct` 0.35 per target (the sl35
   pattern — replaces the 2% account fallback so the funding-normalization
   exit is always the real exit), NO take-profit (the book's right tail is
   the P&L; the 2026-07-28 stop grid in the review §11 is why nothing
   tighter is declared). **Measured directly 2026-08-07**, after the owner
   observed live trades running far in the book's favour and giving it back:
   29 profit-taking cells on the registered v4 scorer — fixed take-profit,
   trail-from-peak, armed trail, half scale-out, and take-profit-then-re-enter
   — and **not one beats the baseline on mean bp/day**. 2026 falls +74.1 →
   +18.9 bp/day at a +40% take-profit. The price leg is −1.45 bp/day against
   the crowd fee's +24.57, so the price run is a cost the book carries, not
   a profit it is failing to bank. A second wave took the owner's
   "sell the top" question — 34 more cells across seven adaptive
   families (fee-gated, vol-scaled, run-fraction give-back, ratchet floors,
   depth-relative funding exit, blow-off spike, carry-vs-gain, volume climax,
   stall) — and **the eight cells that beat the baseline at midnight are all
   decision-hour luck**: 0–6 of 24 clock phases. The best of them loses its own
   placebo, where a **random** exit hour in randomly chosen spells scores
   better than the rule and beats it in 145 of 200 draws. A third wave then
   moved to **1-minute** klines (100% coverage of the book, 2.95M held minutes,
   0 missing parts): the true peaks are ~2x the hourly view (mean +19.83%
   against a mean final of -0.73%), but a volatility spike carries **no signal**
   (Spearman rho -0.013 against the forward return, non-monotone) because the
   spike is the middle of the move -- after a >20% hour the next 60m is +0.99%.
   16 of 17 minute-resolution spike rules lose, and the survivor is beaten by a
   **random** exit minute in 300 of 300 draws. **105 cells across nine families,
   nothing survives**; hold the trigger fixed and vary only the delay and the
   series is monotone, converging to the baseline from below. Ledger row and method:
   `docs/research/research_findings.md` §2; report in
   `~/SHARED_DATA/bybit_full_pit/reports/carry_hold_exit_grid_2026-08-07/`. Sizing: weight × owner equity × profile multiplier
   (0.5 since 2026-08-20, 1.0 when this was measured), per-name 0.10, gross
   cap 1.0, entry leverage 5 (2 then) under the engine risk kernel's
   unchanged caps. **The equity in that product is the equity as
   of the decision, not the live mark** (2026-07-30 change point): sizing off
   the live mark made the day's targets a function of the book's own
   unrealized P&L and rebalanced the whole book every few minutes. Targets are
   constant between decisions, and the resize dead-band is 5% of standing
   notional — below that the tracking error is not worth the round trip.
4. **Live-vs-scored divergence, stated up front**: the research frame drops
   each symbol's terminal 24h (the registered frame caveat, ~+0.13 Sharpe
   in research's favor); the live sleeve cannot and does not dodge — it
   holds through bars the scorer never sees. Forward research rows and the
   live journal are two records; neither substitutes for the other.
   - **Entry price basis (2026-07-30).** §3 scores entries at the decision-bar
     close (`execution_delay_ms=0`). The live sleeve cannot get that price: the
     00:00 UTC bar is only published at ~00:20, so orders go out ~23 minutes
     after the price the headline numbers use. Measured on the two on-time live
     entries, the gap between the modelled and realised entry price was
     **−363.8 bp (`ESPUSDT`) and +111.4 bp (`VANRYUSDT`)** — sign is noise at
     n=2, the dispersion is the point. The edge itself survives this (§5.2
     stress: +1h → t 4.19, +4h → t 4.82), but that means **the live sleeve runs
     the delayed-entry stress case, not the headline case**. Quote forward
     comparisons on that basis.
5. **What the live sleeve does that the scorer does not.** The scorer sets
   weights once per daily bar and lets quantity ride until the next one. The
   producer additionally re-sizes intraday whenever the accepted notional
   drifts more than 5% from target. That overlay is not in any registered
   number. It is now bounded (dead-band, decision-anchored equity) rather than
   continuous — 2026-07-29/30 it fired 340 times in 34 hours, 45% of those
   orders under $100 on a $255k account — but it has not been removed, and
   whether a daily book should track notional intraday at all is an open owner
   decision, not a settled one.
6. **Known sharp edges at deployment** (from the build review): (a) entry
   attempt keys are per-symbol-stable, so one terminal kernel-side
   rejection suppresses that symbol's entries for the journal's life — the
   producer avoids the self-inflicted cases (expired-on-arrival validity,
   dust orders), and a bricked symbol surfaces in the cycles dataset's
   suppression counts; kernel-side attempt versioning is queued. (b) The
   live bar keying is close-time (decision at 00:00 close, computed 00:20)
   — knowledge-content identical to the research row, one grid-phase
   convention apart, inside the registered decision-clock caveat. (c) The
   cycles datasets (~1.4k rows/day) are written one day-bucketed part per
   calendar day, so a cycle append rewrites one day rather than the whole
   history; landed.

## 8. What this document does not claim

Lane-1 numbers selected this rule; only the forward record grades it. Fees are
measured, impact is not. The 2025-26 contribution rides the structural funding
inversion; a normalization shrinks the opportunity set. The Binance arm
replicates the mechanism but does not independently beat the deployed
benchmark's Sharpe on the full-calendar basis (1.66 vs 1.84). Correlation
+0.75 with `lane2_financed_leaders_v1` — the two harvest one macro-premium in
different phases and must not be presented as independent bets.
