# W6 — bybit-first research program

The successor wave to W5 (continuous signal alpha). W5 exhausted the price/return lever
space on the existing panel; W6 opens the orderflow/squeeze axis, execution/cost alpha,
and hedge deepening — bybit-first.

**Date:** 2026-06-15 · **Author:** rob435 (operator-directed) · **Status:** working plan
(not a pre-registration; each binding stage gets its own receipt under
`docs/preregistration/`).

We trade on **bybit**, so bybit is the primary venue here: a signal is worth pursuing
if it is **bybit-robust** and **not completely losing on binance**. Binance stays a
both-venue check where its data allows, and a second-sleeve option where it is the
robust venue (dispersion). Everything is research-stage forward-watch; the **Tier-3
real-money gate is UNCHANGED** and unmet — no item here changes that.

## W6 STATUS — Track A converged, Track B data-gated (2026-06-15)

The squeeze-proxy spine (Track A) was executed and is CLOSED for harvestable modes; all NULL:
- **A1 squeeze SIZING** — NULL (return rises with tilt, DD rises faster → MAR falls; gross-neutral).
- **A4 squeeze × hedge-intensity** — NULL (λ0.5 +0.057 within 8-seed shuffle noise; not λ-robust;
  cost-fragile; BTC-vol stays the unique hedge regime).
- **A5 gross-scaler** — SKIPPED by cheap diagnostic (squeeze-breadth→return weak +0.094, faces A1's DD).
- **Crowding-ADMISSION** — screen POSITIVE (rejected fades profitable +37bp/fade, ledger-validated)
  but the engine sweep is NULL (admitting them LOWERS MAR −0.075/−0.14; at matched gross worse than
  uniform leverage → the admitted fades are tail-correlated; the crowding cap is near-optimal).

**SHARPENED ROOT CAUSE (W5+W6):** the book's edge is diffuse but its TAIL is correlated-concurrent,
so EVERY lever that adds/sizes book exposure concentrates the tail → MAR falls; only the BTC-vol
HEDGE (tail protection without added exposure) harvests. The orderflow OI-squeeze signal is a real
IC (+0.0665) that harvests in NO mode.

**Track B (cost/execution alpha) is DATA-GATED locally:** `tick_ohlc_1m` + all sub-hourly klines = 0
partitions (B1 entry-price timing blocked); binance OI ~6wk (both-venue orderflow + A1/A4 binance leg
vacuous); depth forward-only (B2/B4). The creative long-side fade-of-dumps angle is operator-FORBIDDEN
(bounce products killed; "do not revive").

**In-sample W6 search on local data is COMPLETE.** Per the W5 lesson (consolidate; don't manufacture
low-prior in-sample experiments — Stage-4 false-positive risk), the credible next steps are: (1) DATA
ACCRUAL — binance OI forward tape (E4), sub-hourly price/tick (B1), depth maturation (B2/B4); (2) the
operator-gated funding-guard fix (`docs/audit/2026-06-15-funding-interval-mislabel-guard-falsepositive.md`);
(3) forward-watch the live BTC-vol regime-hedge; (4) a new research direction. Receipts:
`2026-06-15-w6-squeeze-proxy-sizing.md`, `…-squeeze-hedge-intensity.md`, `…-crowding-admission.md`.
Scripts: `scripts/w6_squeeze_proxy_sizing.py`, `…_squeeze_hedge_intensity.py`,
`…_crowding_admission_screen.py`, `…_crowding_admission_sweep.py` (all uncommitted, operator-gated).

## The one lesson that shapes every track

W5 (~18 mechanisms) converged to a single root cause: **the continuous fade book's
edge is DIFFUSE and the book PROFITS when broadly deployed in dislocations**
(daily concurrency correlates *positively* with daily return, +0.155 bybit; the best
days are the most concurrent). Therefore every lever that **selects / shrinks /
prioritizes / derisks the entry set forgoes the diffuse profit** and fails to harvest
(entry priority, path-shape, liquidity sniper, decel, funding selection, sizing-down,
concurrency caps, both exit directions). The only thing that harvested was an
**overlay that keeps the whole book and hedges the squeeze tail** (the BTC-vol
regime-hedge, now live).

**Strategic consequence — only chase harvests that DON'T fight the diffuse edge:**
1. **Overlays that keep the whole book** — hedge-intensity conditioning, gross/leverage
   scaling, exit *timing* (not earlier exits). ✅ proven mode (regime-hedge).
2. **Cost / execution alpha** — better fills, maker economics, capacity. Lifts net MAR
   without touching the signal. ⬅ entirely unexplored, high prior.
3. **Sizing-UP the best fades** (mean-1, gross-neutral) — the ONE selection-flavored
   lever still open, because it adds rather than removes deployment. Guarded prior
   (Stage 5 path-shape sizing failed; the squeeze proxy is a fresh, stronger IC).
   AVOID anything that shrinks breadth or drops names — proven dead.

The orthogonal signal axis W5 never used is **orderflow / squeeze context** (OI,
funding, liquidations, depth, LSR). That is the spine of this program.

## Ranked roadmap (bybit-first)

| P | Item | Mode | Data | Effort | Gate |
|---|---|---|---|---|---|
| **P0** | A1 OI-buildup squeeze **sizing** sweep | size-up | ✅ local | ~1–2h run | receipt filed |
| **P0** | A4 squeeze × **hedge-intensity** (compose w/ live regime-hedge) | overlay | ✅ local | ~1h | new receipt |
| **P1** | A5 **gross scaler** — deploy MORE in squeeze-rich regimes (Stage-9 flip) | overlay | ✅ local | ~1h | new receipt |
| **P1** | B1 **intrabar entry timing** (tick_ohlc_1m / taker_flow_5m) | cost | ✅ local | ~2h | cost-delta |
| **P1** | A3 squeeze **composite** score (OI+funding+LSR) | feature | partial | ~1h | screen |
| **P2** | A6 **squeeze-completion exit** (re-cast the dynamic-exit mirage) | timing | ✅ local | ~2h | receipt |
| **P2** | B2 **maker/passive** resting-ladder economics (R4/depth) | cost | 🧱 depth | ~3h | cost bar |
| **P2** | C1 regime-hedge **forward calibration** (λ, sub-period, binance headroom) | hedge | forward | ongoing | F-bar |
| **P2** | D1/D2 revive path-shape & liquidity IC via **non-selection** harvest | mixed | ✅ local | ~2h | controls |
| **P3** | A7/A8/A9 liquidation / book-thinning / LSR squeeze legs | feature | 🧱 forward | gated | screen |
| **P3** | B4 **capacity/impact** curve (depth) | risk | 🧱 depth | ~2h | — |
| **infra** | E1 binance liquidation host · E4 binance OI tape · E3 P11 | data | operator | — | — |

---

## Track A — Squeeze-proxy program (the spine)

Thesis: the book SHORTS pumped names; a pump on a CROWDED long (OI buildup, extreme
funding, live liquidations, thinning book, lopsided LSR) is a squeeze that fades
harder. The exploratory screen already confirmed real within-symbol IC over the
composite (symbol-hash control degenerate both venues):
`oi_chg_24h` **bybit +0.0665 p=0.002** (all thirds +); `funding_level` **binance +0.056
p=0.013**, bybit +0.025 same sign. Admissibility ≠ harvestable — the engine stages decide.

- **A1 — OI-buildup sizing tilt (P0, pre-registered).** Mean-1, gross-neutral size-up
  of high-squeeze fades via the merged `size_mult_lookup` hook; multi-seed shuffle +
  random controls, cost stress, thirds, bybit-robust bar. Receipt:
  `docs/preregistration/2026-06-15-w6-squeeze-proxy-sizing.md`. **Run next.**
- **A2 — funding both-venue.** Same harness, `sq = z(funding_level)` (the same-sign
  both-venue leg). Cheap add-on to A1's grid.
- **A3 — squeeze COMPOSITE.** `sq = w1·z(oi_chg_24h) + w2·z(funding) (+ w3·z(LSR))`,
  weights frozen from the screen ICs (no in-sample weight tuning — use IC-proportional
  or equal weights, pre-stated). One score to drive A1/A4/A5/A6.
- **A4 — squeeze × hedge-intensity (P0, high prior).** The live hedge already scales by
  BTC-vol regime; **also** scale it by aggregate book-squeeze (sum/mean of per-name
  squeeze scores that day). Rationale: a day where the whole book is short crowded
  squeezes is exactly a market-wide squeeze-risk day → hedge more. This is the
  proven overlay mode and composes with the deployed `hedge_intensity` hook
  (multiply the two mean-1 intensities). Strongest non-selection bet.
- **A5 — gross/leverage scaler (P1, the Stage-9 flip).** Stage 9 sized the book DOWN
  in high vol and failed *because the book profits in high vol*. Test the inverse: a
  causal, mean-1 **gross scaler** that deploys MORE when many concurrent squeeze setups
  co-occur (breadth-of-squeeze), staying within the existing max_scale/DD guards.
  Directly operationalizes the concurrency insight. High prior precisely because it
  does NOT shrink the book.
- **A6 — squeeze-completion exit (P2).** The dynamic-exit mirage (bybit +1.74,
  2026-carried) was a price-target fade-completion TP. Re-cast it **structurally**:
  exit (or trail) when the *squeeze resolves* — OI unwinds back toward pre-pump, the
  liquidation cascade ends, funding normalizes — rather than a price level. An exit
  *timing* overlay (keeps the position through the squeeze, exits on structural
  completion) does not fight the diffuse edge the way earlier-exit did (Stage 3/3b).
  Decompose returns into price-fade vs funding-carry first (the short earns funding on
  high-funding names — see A2) to see which leg the squeeze proxy predicts.
- **A7 liquidation-cluster · A8 book-thinning · A9 LSR-crowding (P3, data-gated).**
  Add as causal features once the forward tapes mature (liquidation ~2026-07-10; depth
  ~weeks; LSR history shallow). Frozen design in
  `docs/research_plans/w6_bybit_program/orderflow.md` §2. Each enters the screen,
  then A1/A4 if admissible.

## Track B — Execution / cost alpha (bybit microstructure, under-explored)

The base book pays ~15–20 bp/trade. Cutting that lifts net MAR with ZERO signal change
and zero diffuse-edge conflict — the highest-leverage untouched axis. bybit has
`tick_ohlc_1m` and `taker_flow_5m` (intrabar microstructure) and a live depth collector.

- **B1 — intrabar entry timing (P1).** The engine enters at a fixed +1h-delay bar
  close. Use tick_ohlc_1m / taker_flow_5m to time the fill WITHIN the entry window
  (e.g., wait for taker-buy exhaustion / a local high) to get a better short entry
  price. Measure as a per-trade entry-price improvement vs the current convention,
  causally; it must hold both directions and survive realistic latency. Pure cost alpha.
- **B2 — maker/passive resting-ladder economics (P2, depth-gated).** Naive passive-at-
  touch was null (adverse continuation tails ate the maker savings), but deeper resting
  ladders are the one passive form still alive. With matured depth data (R4), calibrate
  whether a resting-ladder short can clear the cost bar. Only worth it if depth shows
  the fill probability × adverse-selection trade-off is favorable.
- **B3 — sniper re-cast continuous (bybit).** The decile-drop sniper was falsified on
  bybit (Stage 4d), but the within-symbol liquidity IC is *real* (+0.081, p=0.001). The
  failure was the harvest (drop a decile), not the signal. Try a *continuous,
  cost-aware* harvest that does NOT drop names: size/time entries by liquidity (thin
  book → smaller clip / passive; deep book → fuller clip), folding liquidity into B1/B2
  rather than a selection filter.
- **B4 — capacity / impact curve (P3).** Depth-calibrated: how much notional can the
  book hold per name before impact eats the edge. Essential for any real-money path and
  for sizing A1/A5 honestly (resize/impact cost is already modeled — calibrate it).

## Track C — Hedge program deepening (protect & extend the deployed edge)

- **C1 — regime-hedge forward calibration (P2, ongoing).** The BTC-vol regime-hedge is
  live but characterized as modest/sub-period-variable. Forward-watch: does intensity
  fire in real squeeze episodes; is realized demo DD reduced; does binance turnover cost
  stay within model (the thin 1.5×-cost headroom is the lone caveat). Forward-calibrate
  λ; consider a **convex** intensity (scale hard only in the top vol decile) so the
  binance cost is spent only where the benefit concentrates.
- **C2 — bybit-specific second hedge signal.** Book-DD was binance-only; dispersion is
  binance-robust/bybit-noise. The open question: is there a *bybit* complement to
  BTC-vol? Test the **aggregate book-squeeze score (A3) as a hedge regime** on bybit —
  hedge more when the book is collectively short crowded squeezes (this is A4 stated as
  a hedge-signal rather than an intensity multiplier; evaluate both framings).
- **C3 — hedge structure.** State-dependent λ, a dynamic cap, or a two-regime
  (calm/turbulent) hedge object. Low prior individually; bundle as a single forward
  calibration with C1.

## Track D — Finish W5 leftovers through the new lens

- **D1 — path-shape (~0.10 IC) harvest, non-sizing.** Admissible (Stage 7b), not
  harvestable via sizing (Stage 5). Retry as an **exit-timing** or **hedge-conditioning**
  input (Track A6/A4 modes), not selection/sizing.
- **D2 — liquidity IC harvest, non-selection.** See B3.
- **D3 — dynamic-exit forward shadow (F1/F2/F3).** Check whether the bybit shadow has
  ≥60 forward days / ≥40 arms yet; if so, evaluate F1 (beats real per-trade) + F2 (both
  halves). If it passes, A6 is partly de-risked; if it fails, retire it. Cheap, just
  reads the shadow JSONL vs the live ledger.
- **D4 — dispersion hedge (binance).** Real binance hedge (+0.293); only if a binance
  sleeve runs. The BTC-vol×dispersion stack is binance +0.368 → the natural per-venue
  hedge (BTC-vol bybit + dispersion binance).

## Track E — Data & infra (unblocks B/C/A7–9 and binance both-venue)

- **E1 binance liquidation host** (operator) — start the binance forward liquidation
  tape; runbook in `docs/research_plans/w6_bybit_program/orderflow.md` §3. Every day
  unfixed is unbackfillable binance history.
- **E2 depth maturation (R4)** — bybit depth live since 2026-06-13; gates B2/B4.
- **E3 P11 taker-flow completion** — richer taker features for a re-test (idle-time).
- **E4 binance OI forward tape** — binance OI is ~6 weeks; accrue forward so the
  OI-squeeze (A1) can become both-venue instead of bybit-only.
- **E5 positioning_lsr history** — bybit LSR (toptrader/global/taker) is shallow (~66
  partitions); accrue forward so A9 / A3's LSR leg gains history.

## Track F — Real-money readiness (the long game, gated)

Nothing promotes to real money without the three-tier demo-arbiter gate (forward demo +
cross-venue bar, funding costed). The prerequisites this program produces: B4 capacity
curve, B1/B2 realized-fill/slippage calibration, C1 forward hedge evidence, and ≥30
forward demo days per book. Keep `REAL_MONEY=false`.

## Methodology guardrails (non-negotiable, banked from W5)

- **Multi-seed nulls.** Single-seed random controls have huge MAR variance (the Stage-4
  trap) — every harvest claim needs ≥5-seed shuffle/random controls.
- **Admissibility ≠ harvestable.** A real IC (path-shape, liquidity, OI-buildup) is a
  screen pass, not an edge. The engine intervention + controls + cost stress decide.
- **Pre-register before the run.** Any stage touching the full-PIT roots gets a dated
  receipt committed in the same PR (AGENTS.md). Screens are exploratory.
- **bybit-robust across the free parameter**, not bybit-positive-in-one-cell (the
  sniper trap); ≥2/3 chronological thirds; survive 1.5× cost stress.
- **Causal / PIT always.** Every feature reads strictly-before-decision data; verify the
  symbol-hash negative control is degenerate on every screen.
- **Forward demo is the arbiter.** In-sample is for prioritization; promotion needs
  forward evidence over a real (squeeze) sample. Tier-3 stays strict.

## Suggested execution order
1. **A1** sizing sweep (data in hand, receipt filed) — the live evidence path.
2. **A4** squeeze × hedge-intensity (composes with the deployed hedge; highest-prior
   overlay) + **A3** composite as its input.
3. **A5** gross scaler + **B1** entry timing (two orthogonal, diffuse-edge-friendly
   harvests).
4. **D3** dynamic-exit shadow check (cheap) + **C1** hedge forward calibration (ongoing).
5. **E1** binance liquidation host (operator) to unblock the binance/both-venue legs.
6. **B2/B4** maker economics + capacity once depth matures; **A7–9** when tapes mature.
