# Alpha-hunt charter: keep sharpening the continuous system (2026-06-10)

**You are a Jane Street-style quant inheriting a working-but-blunt strategy. Your job is
to make it genuinely better — relentlessly, creatively, honestly. Failure of any single
idea is a first-class result; giving up is not. But "better" means a REAL, both-venue,
cost-and-funding-honest improvement that survives out-of-sample — never a loosened bar.**

The current system works (it's banked research on demo), but it is *unsophisticated* in
several places a real desk would never tolerate. Those are your hunting grounds.

---

## 0. Read first (do NOT re-derive — prior sessions did the work)

- `STATE.md` + `docs/research_summary.md` — program state + binding decision rules.
- `docs/backtesting_errors_we_never_repeat.md` — the methodology standard (mandatory).
- Memories: `continuous-refinement-campaign-2026-06-09`,
  `continuous-winner-robustness-2026-06-09`, `continuous-weight-overfit-dead`,
  `continuous-btc-hedge-stage-a-pass`, `continuous-rs-gate-dead-hedge-instead`,
  `downtrend-sniper-program-2026-06-09`, `continuous-live-readiness-program`,
  `alpha-hunt-2026-06-03`, `p3-residual-momentum-lead`, `p2-1-mostly-factor-exposure`.
- The 2026-06-09/10 receipts under `docs/preregistration/continuous-*`, `sniper-*`,
  `wp4-*`, `downtrend-*` (some receipts since consolidated into
  `docs/research_summary.md`; git history is the archive).
- **Invoke the `backtest-integrity` skill before any run and `research-phase-runner`
  for the pre-register → run → verdict → STATE-update loop.** Both venues, always.

## 1. The current object (what you're improving)

Continuous short book = 4-component event ensemble
`{turn3p3:.30, turn4p3:.20, turn4p5:.40, age210tp14:.10}` (turnover-spike + pop triggers,
decile-9 of a single composite feature `max_ret168`, rmom-q25 gate, BTC-30d-uptrend gate,
age≥240, liq≥$500k), per-name inverse-vol sizing at 2% notional, `w90/tv0.045/max4/ddh-0.04`
daily rebalance, TP10/TP14 + fixed 24h hold + breakeven/failed-fade exits, wide 0.25 disaster
stop. Banked overlay: causal 90d-beta long-BTC hedge. Tier-1 lead: +8% "sniper" resting-limit
add. Engine: `liquidity_migration.continuous_{events,rebalance}`; live: `continuous_demo*`,
`continuous_hedge_manager`.

## 2. The ONE durable lesson (3 independent confirmations: D2/D3/WP4)

**This system's edge is EVENT SELECTION + EXECUTION. Daily cross-sectional *standalone*
books on this data fail on drawdown-class and cost/funding at our scale. New alpha comes
from better SIZING, better EXECUTION, better HEDGING, or NEW DATA — not from new
recombinations of the daily/hourly price-volume data already mined.** Orient the hunt there.

## 3. Already dead — do NOT re-mine (each has a pre-registered NULL)

Alt-RS / BTCDOM entry gating (alt-RS is a daily martingale); within-pool ridge combiner
(negative OOF IC); standalone funding/vol/lottery/long-low-vol/trend factor shorts;
unconditional & downtrend cross-sectional reversal L/S; rmom standalone L/S book; downtrend
bounce-long sleeve; ensemble weight re-optimization (equal-weight matches the winner OOS —
weights are NOT the edge); parsimony/multi-horizon-blend/half-life-timing/entry-circuit-breaker;
hard funding/premium/basis filters. If you revisit any, you need a NEW mechanism or NEW data
and a fresh pre-registration — not the same idea reskinned.

## 4. The hunting grounds — what's UNSOPHISTICATED and easily better (ranked by EV)

Pick one, pre-register a falsifiable bar, attack it. These are real desk-grade upgrades, not
tweaks. Order is a suggestion, not a constraint — follow the evidence.

**A. SIZING via a real risk model (highest EV).** Per-name inverse-vol ignores
cross-correlation — the book is a pile of alt shorts that squeeze *together* (that's why
loosening the rmom gate "added correlated breadth and blew out DD"). That's a sizing failure,
not a signal failure. Build a covariance-aware sizer (shrunk/Ledoit-Wolf covariance or a
few-factor PCA risk model of the alt cross-section) and size to a risk target on the PORTFOLIO,
down-weighting correlated names. Hypothesis: same names, materially better DD/Sharpe and a
higher safe leverage, on both venues. This likely also lets the rmom gate become a continuous
tilt (short the weakest MORE) instead of a binary q25 cutoff that throws away a monotone signal.

**B. EXECUTION alpha — generalize the sniper (high EV, also fixes capacity).** The sniper
proved passive limit entries beat market entries (+2-3%/fill). That's the tip: rebuild entries
as a smart-execution layer (post-only / passive-first, then escalate; TWAP/POV slicing; the
+wick ladder generalized to a full passive schedule). This harvests spread instead of paying it,
and directly attacks the two worst weaknesses (cost stress + ~$200-300k capacity). Pair with
**participation-capped sizing** (cap each name at X% of its hourly turnover) — a small modeled-MAR
give-up for plausibly 5-10x deployable capital, which was flagged and never built. Calibrate the
fill/impact model against the live demo fills now accruing (the R4 calibration).

**C. Multi-factor / smarter HEDGE (medium EV, banked base to beat).** The live hedge is one
noisy 90d OLS BTC beta. Improvements: shrinkage/Bayesian beta; an ETH+BTC or tradeable
alt-basket hedge (alt_ew had the higher ΔSharpe but wasn't tradeable at full breadth — find the
tradeable middle); hedge the top PCA factor(s) of the cross-section, not just BTC. Bar: beat the
banked BTC-hedge's variance reduction on BOTH venues without a sample-specific return crutch.

**D. DYNAMIC EXITS (medium EV).** Fixed 24h hold + fixed TP is a clock, not a thesis. The fade
plays out at a vol- and velocity-dependent rate. Test vol-scaled / fade-velocity-scaled holds,
or exit-on-fade-completion (mean-reversion target) vs the clock. Same "stops sold the wick"
sophistication the sniper applied to entries, applied to exits.

**E. FACTOR-NEUTRAL construction (medium EV, answers the alpha-vs-beta question).** P2-1 found
the book is mostly factor premia. Build a sector/size/beta-neutralized short book (short the
relative losers *within* peer groups) to isolate the idiosyncratic fade residual from
uncompensated factor bets. If the residual survives both-venue net of costs, that's the clean
alpha; if not, that's a real (publishable-internally) finding about what the strategy actually is.

**F. NEW DATA — the only path to a *large* step (high EV, higher effort).** The signal is
price+volume daily/hourly. The fade is fundamentally a squeeze/liquidation phenomenon, so the
highest-signal new data is liquidation feeds, order-book depth/imbalance, taker flow, perp basis
term-structure, options skew, and on-chain flows. Liquidation-cascade timing is the most direct.
Scope acquisition + a PIT-clean ingestion before modeling. This is where a genuinely new,
higher-capacity alpha most plausibly lives.

**G. REGIME model beyond one binary gate (lower EV, enabler).** The whole book's aggressiveness
keys off a single BTC-30d sign. A proper regime read (vol regime, cross-sectional dispersion,
funding regime, trend strength) conditioning leverage/breadth could lift the regime-robustness
the hedge only partially fixes.

## 5. The bar (non-negotiable — this is what makes a "win" real)

- Pre-register every full-PIT run with an a-priori falsifiable bar BEFORE running it.
- **Both venues.** A single-venue (usually bybit) win is a mirage — the program has been
  fooled by it before; cross-venue agreement is the robustness test.
- Tier-2 demo-candidate = positive return both venues, pooled MAR-Δ > +0.1, neither venue
  < −0.5, survives 2× cost, fragility diagnostics REPORTED. Tier-3 (real money) stays strict
  and is decided ONLY by forward demo/paper — never loosened to rescue a result.
- Demo/paper only. `REAL_MONEY=false`. Do NOT commit/push or change live deploy without the
  operator's explicit say-so (a push auto-deploys to the live VPS).
- Anchor leverage to max4-6, not the recent-regime-flattered max10.
- A clean NULL, pre-registered and explained, is a first-class deliverable — it stops the next
  agent wasting effort. Write it up with the same care as a win.

## 6. Reproduction & tooling (so you don't rediscover it)

- Data roots — SUPERSEDED, see STATE.md + docs/data_roots.md (dated charter:
  the Windows paths below were the 2026-06-10 box; the current dev box uses
  `~/SHARED_DATA/{bybit,binance}_full_pit`, and the roots were extended
  2026-06-10 — bybit to 2026-06-09, binance to 2026-05-31 — so the `--end
  2026-05-27` cap is stale). Original text: `C:\Users\user\SHARED_DATA\{bybit,binance}_full_pit`; cap `--end
  2026-05-27`; `PYTHONPATH=...`, `POLARS_MAX_THREADS=8`, `.venv/bin/python`,
  `PYTHONIOENCODING=utf-8`. Verified daily panel builder: `scripts/continuous_rs_squeeze_probe.py`
  `load_daily_panel` (float-exact daily close/turnover per symbol; the cheap, trustworthy primitive).
- Component ledgers + `combine_continuous_components` + `apply_rebalance_rule` (+ the hedge leg)
  are the engine; `scripts/continuous_hedge_engine_driver.py` shows the end-to-end metrics path;
  `scripts/continuous_walkforward_allocator.py` is the causal-OOS harness to copy for any
  "is this selection-overfit?" check. `scripts/sniper_entries_bar_accurate.py` shows bar-accurate
  fill simulation from raw 1h klines (reuse for execution work).
- Forward evidence: `liquidity_migration/continuous_forward_replay.py` (no-order, exact-engine replay; config-hash
  pinned). Live calibration input: the continuous demo fills now accruing on the VPS.

## 7. Mindset

Be the desk that does NOT ship the backtest that can't survive its own cost stress — and also
the desk that never stops hunting for the version that can. Every blunt edge in §4 is an
invitation. Attack one, prove it honestly on both venues, bank it or kill it cleanly, write it
down, pick the next. Make us proud.

---

## 8. Forward pipeline (added 2026-06-10 by operator instruction: "always have alpha to work on")

§4 status after session 1 (receipts in `docs/preregistration/`, roll-ups in the summary):
**A** DEAD a priori (book too thin — median 1 peer at entry; don't re-mine at current
breadth). **C** DONE/BANKED (BTC+ETH two-factor hedge, Stage-A 6/6 + Stage-B engine s0-s8;
shrunk-beta and 50/50-basket estimator families closed). **B-sizing** NULL (participation
cap fails dominance on bybit — thin names carry MAR; capped frontier recorded as the
trust-region menu; don't re-mine without R4 calibration). **F-scoping** DONE (see
`docs/research_notes_new_data_scoping_2026-06-10.md`).

Queue, ranked by EV × runnability. Every item gets its own pre-registration; the §5 bar is
unchanged; a NULL retires the item with a receipt.

**P1. Liquidation-squeeze PROXY features (the §4-F build, highest new-alpha EV).**
Raw liquidation history is unbuyable (archives deleted/never published; feeds 1/s-sampled).
Build the cross-venue PIT-clean proxy instead: (a) backfill Binance Vision `metrics`
(5-min OI + taker ratios, 2020-09→) into the research root — this also satisfies the
pre-registered ridge-rerun precondition; bybit OI is already full-history locally;
(b) construct causal squeeze-proxy features (OI-drop × adverse-move bursts, taker-flow
imbalance spikes) on the hourly grid; (c) ONE pre-registered test: do proxy-conditioned
entries/sizes improve the continuous book both-venue? Falsifier-first design; the WP1a
lesson says squeeze TIMING at daily granularity is dead — the proxy must be intraday and
event-anchored, not a daily gate.

**P2. Execution layer, demo-calibrated (the §4-B execution half).** Blocked on R4 (VPS
demo fill-ledger pull — operator). Once fills exist: calibrate maker-fill odds + real
impact, then generalize the sniper into passive-first entries (post-only → escalate),
re-test the failed +0.5-at-2x sniper margin with REAL fill data, and revisit the
participation-cap B3 question with calibrated impact. High EV, mostly engineering.

**P3. Live liquidation/flow collectors (forward-only data).** `allLiquidation` (bybit) +
`forceOrder` (binance) WS collectors on the VPS — forward history is unbuyable later;
every week of delay is a week of lost future OOS data for P1's successor. Operator/deploy
decision; spec is small (append-only JSONL, idempotent restart, no order path).

**P4. Factor-residual attribution of the HEDGED continuous book (§4-E, analysis).**
P2-1 answered this for the daily book (mostly factor premia). Run the same decomposition
on the 2f-hedged continuous book: how much of its Sharpe is residual after STR/market/
size? Informs the Tier-3 residual-Sharpe gate before forward demo matures; pure analysis,
no new strategy surface.

**P5. Dynamic exits (§4-D, one shot, low prior).** Exit-on-fade-completion vs the 24h
clock (vol-scaled target hit OR time stop). The exit family has a deep graveyard (TP
grid, trailing, giveback, breakeven, rank-decay all null) and multi-horizon showed 24h is
THE cross-venue horizon — so a single pre-registered falsifier-first shot only; a null
closes §4-D permanently.

**P6. Regime model v2 (§4-G, parked).** Needs forward data to be honest (the 2023-26
window is spent for regime fitting). Revisit when the forward replay clock has ≥60 days.

**Standing operator items (not agent-runnable) — SUPERSEDED: all five executed 2026-06-10, see STATE.md "Operator queue — EXECUTED":** R4 fill pull (`bash
scripts/reconcile.sh`), data-root refresh (starts all forward clocks), 2f hedge second
leg in `continuous_hedge_manager`, commit/push of the session tree, P3 collector deploy.

Rule for future sessions: when the queue's runnable items are exhausted, extend this
section FIRST (with ranked, falsifiable, non-dead-list items), then work it top-down.

### Wave 2 (added 2026-06-10 after P1/P4/P5 concluded; P5 closed §4-D permanently)

Status: P1 Stage-1 info PASS / Stage-2 tilt NULL (daily-tilt conversion closed);
P4 done; P5 NULL (§4-D closed). Runnable queue, ranked:

**P7. Passive-first entry LOWER BOUND (the §4-B execution half, runnable pre-R4).**
The P5 work left a 100.0%-parity bar-accurate replay primitive. Use it to test
passive-FIRST entries: replace the taker fill at entry with a resting limit at the
signal close (maker rebate/zero-spread side), escalate to taker at T+1h if unfilled —
under a pre-registered PESSIMISTIC fill rule (filled only if the bar trades strictly
THROUGH the limit). If even the lower-bound fill assumption clears Tier-2 vs the
taker-entry control, passive entries are real before any calibration; R4 demo fills
later upgrade the realism. One shot, both venues, full cost stress.

**P8. Down-only OI de-sizing (the one legitimately-open Stage-2 successor).**
Asymmetric: NEVER up-size (avoids the variance-cost failure that killed the tilt);
only de-size pops with FALLING OI (squeeze-already-spent events — Stage-1's worst
tercile). m = 1 for ΔOI_6h ≥ 0; m = 0.5 for ΔOI_6h below the trailing-90-event 25th
percentile (causal); else 1. Gross loss is small and one-sided; judged at full Tier-2
+ the ±5% gross guard logic adapted (expected gross ratio ~0.95-0.98 — pre-state the
band). One shot. Lower prior than P7.

**P9. Binance `bookDepth` ingestion (data layer only, no signal claim).** Free,
2023-01→current, ±1-5% depth bands. Binance-only → can never clear a cross-venue bar
alone; ingest + document for execution-cost modeling (P7/P2 realism) and as a
forward-data complement. No pre-registration needed until a signal test is proposed.

Parked/blocked: P2 full execution layer (R4), P3 collectors (operator), P6 regime-v2
(forward data), W2 cross-venue OI confirmation (multiple-testing creep on the new
dataset — needs a strong independent prior first).

### Wave-2 outcomes (2026-06-10, same session) + program state

**P7 NULL** (passive-at-touch fills ~always at 1h; continuation tail destroys the risk
profile; the sniper's wick-deep ladder is THE working passive form). **P8 NULL** (the
OI sizing arc is fully closed — tilt and down-only both dead at daily granularity;
Stage-1's event-level information stands). **P9 LAUNCHED** (same session, idle-time):
`scripts/backfill_binance_bookdepth_vision.py` ingests hourly-aggregated per-band
depth (mean+last notional/depth per symbol×hour×band) into
`binance_full_pit/binance_usdm_bookdepth_1h/`; resume-safe; smoke-verified.

**Program state after Waves 1-2: every agent-runnable in-sample direction in this
charter now has a pre-registered receipt** (banked: 2f hedge; passes: OI Stage-1
information; nulls: cov-sizer, participation-cap dominance, shrunk/basket hedges, OI
tilt, OI down-size, dynamic exits [§4-D closed permanently], passive-at-touch
entries; analyses: P4 attribution, capacity frontier). The 2023-04→2026-05 window is
mined out across sizing, execution, hedging, exits, and the free new-data surface —
consistent with the §2 lesson and the window freeze. **New evidence now comes only
from: (1) operator actions — R4 fill pull, data-root refresh (starts all forward
clocks), 2f-hedge live wiring, P3 collectors, commit of this tree; (2) forward
demo/paper accumulation; (3) new data layers (P9 spec, taker-flow tick stack).**
Future agents: do not re-mine; work the operator list and the forward clocks.

### Wave 3 (added 2026-06-12: the taker-flow stack — the last unbuilt free data layer)

Authority: standing /goal autonomous-research directive (operator-set, 2026-06-12)
+ this charter's own rule ("extend this section FIRST, then work it top-down").
External priors assembled BEFORE any in-repo test:
`docs/research_notes_external_priors_2026-06-12.md` (taker-flow imbalance is the
dominant short-horizon crypto-futures feature family; manipulated pumps revert with
identifiable volume composition; forced-covering rallies fully reverse; practitioner
CVD-divergence corpus; OI feeds lag during cascades ⇒ flow-primary designs). Ranked:

**P10. Taker-flow squeeze-proxy Stage-1 (event-anchored information test, runnable
NOW).** The untested half of the liquidation proxy — Stage-1/Stage-2/P8 only ever
tested the OI leg. New data layer: `bybit_full_pit/taker_flow_5m` (5-min signed
taker flow from public.bybit.com side-flagged tick archive, survivorship-FREE where
bybit OI was survivor-only; event-anchored coverage, builder
`scripts/bybit_taker_flow_backfill.py`) + binance `metrics_5m` taker ratio (already
local, survivorship-free). ONE primary feature (`flow_support_6h`), two-sided with
cross-venue sign agreement as the gate (the sign-prior conflict is documented ex
ante in the priors note), lag/window/turnover-proxy/OI-proxy falsifiers. Receipt:
`docs/preregistration/continuous-taker-flow-scout-2026-06-12.md`. A PASS arms a
Stage-2 per-event ENTRY-VETO design (fresh receipt); explicitly NOT a size tilt
(closed family). A FAIL retires event-level taker-flow conditioning on this window.

**P11. bybit taker_flow_5m → full-universe completion + forward maintenance (data
layer only, idle-time).** Extend event-anchored coverage progressively toward the
full kline universe; absence-auditable manifest; no signal claim, no receipt until
a test is proposed. Insurance value: public archives get deleted (Binance 2024-Q2
precedent); the tick tape is the one layer that cannot be reconstructed later.

**P12. Liquidation-proxy honesty calibration (blocked ~30d: needs forward
`allLiquidation` capture to mature).** Measure whether taker-burst × OI-drop
proxies actually coincide with REAL liquidation events on the forward bybit
collector tape (started 2026-06-10). Measurement receipt only, no alpha claim —
makes any future proxy-conditioned claim honest, and prices the OI-lag caveat.

Parked/blocked (unchanged): P2 execution layer (R4), P6 regime-v2 (forward data),
W2 OI cross-venue confirmation (the new priors note supports the FLOW family, not
OI re-tests — prior 5 argues against OI-primary intraday designs), PE2 OOS
re-judgment (armed, data-refresh-triggered), TA1 forward-watch leads (≥100
trades/book).
