# Research-program state

**Last updated:** 2026-05-31. **All research findings, results, and verdicts now live in
one place: [docs/research_summary.md](docs/research_summary.md).** Round 1 + Round 2
per-phase plans and verdicts were consolidated there and removed (git history has the
originals). This file = live/operational state + the binding decision rules.

> First session in this repo? Read this file, then `docs/research_summary.md`.

## Current status (one paragraph)

Bybit (+Binance) liquidity-migration **short**, research-stage — the live demo + paper run
a frozen "promoted" profile; NOT real money. **Framing (E1-corrected 2026-05-30):** the alpha
is the **SELECTION** signal (the liquidity-migration event = candidate pool) + a plain +1h
short. E1+E1b **falsified the EXECUTION half** — fade-confirmation entry adds no robust
cross-venue premium over immediate entry, so **E3 (sniper) is dropped** and the open lead is
**SELECTION refinement** (the age gate + residual-momentum gate, under "What's running"). (The
earlier "Round 2 = null" was a methodology artifact — worst-case fills + over-concentration.)
**Intraday-detection kernel — arc CONCLUDED 2026-05-31** (`docs/research_plan_intraday_kernel.md`;
full balanced write-up `docs/intraday_burst_synthesis.md`). K0 confirmed the daily entry is ~8–11%
below the event-day peak (optimistic ceiling); K1a falsified running the *daily selector* hourly
(its ≥6×-daily-turnover rule can't confirm until ~15:00, after the fade). The operator-directed
reopening (I-phase) built a purpose-built intraday burst-short: **I1b = PASS** (real, beta-neutral,
cross-venue signal) **but funding (I2g–I2k) eats ~85% of the edge** — under FAIR (funding-to-exit)
accounting it is only **marginally positive** (24h/25%, MAR +0.30 bybit / +0.49 binance), recent-tilted
(bybit underwater ~3y), found after extensive search (weak evidence; verdict swings
−0.54→+0.30→+3.08 with funding accounting = at the proxy's resolution limit). **Net: fill-timing dead
(E1), same-selector detection dead (K1a), standalone intraday burst-short = MARGINAL + unvalidated;
the only remaining intraday step is the operator-gated engine-grade I3 (a coin-flip — deprioritised).**
The robust, already-validated edge is the **DAILY age+rmom selection refinements** under forward demo
(operator-gated) — their late next-day entry sidesteps both the intraday squeeze and the funding
crowding. **Open daily lead (pre-registered, run-pending): the age+rmom+ff6 combined cell** — the
three separately-validated refinements have never been measured as one stack (do they add or overlap?);
receipt `docs/preregistration/age-rmom-ff6-combined-2026-05-31.md`. Data note: derivative channels
verified (premium/funding both venues full; OI bybit-only; taker binance-recent-only) — see the
corrected memory.
Numbers + full record (the dated source of truth): **`docs/research_summary.md`**. Nothing is
promoted; forward demo is the arbiter.

## What's running

- **SHORT sleeve `drop_all_4` promotion (2026-05-30, OPERATOR OVERRIDE — code-complete,
  deploy pending owner push):** the `promoted` profile drops the 4 non-earning vetoes/bounds
  (`day_return` floor, `stop_pressure`, `realized_loss`, `universe_rank_max`) and runs
  `max_active=12` (systemd `MAX_ACTIVE_SYMBOLS` 3→12 on demo+paper). ⚠️ **FAILS the Tier-2
  cross-venue guard under the corrected engine** (binance net-negative; the deleted research's
  "winner" was an optimistic `stop_fill='stop'` artifact). Deployed by explicit operator
  override for forward-demo observation only — revert if binance stays negative. `strategy_id`
  unchanged → deploy date = clean pre/post split. Receipt + numbers:
  `docs/preregistration/drop-all-4-promotion.md`.
- **LONG sleeve `div` promotion (2026-05-30, code-complete, not yet deployed):** the
  `MultiStratV1` long-FC profile (`_v11a_long_native_config`) gained the `div`
  risk-engineering — universe 10→50, max_concurrent 5→10, de-risk-only vol-target
  (0.60 annual, max_scale 1.0). Cross-venue confirmed (both venues MAR up, DD lower,
  trades ~2×; figures in `docs/preregistration/div-promotion.md`). Portfolio construction, NOT a new signal
  (FC remains the alpha ceiling). Receipt: `docs/preregistration/div-promotion.md`.
  On deploy, the deploy date is the clean pre/post split for the `MultiStratV1` long
  ledger (the strategy_id was kept; a 12h sleeve was tested and **rejected** — additive
  on Binance but a drag on Bybit, fails the cross-venue bar).
- **Live demo** (Singapore VPS 5.223.42.109): `event_demo_daemon` + `ws_risk_daemon` +
  `long_native_event_demo_daemon` under systemd. Frozen promoted profile. Ledgers in
  `data/bybit-demo-event/`.
- **Paper shadow** (same VPS, same profile, no order submission): `data/bybit-paper-event/`.
- **No research runs in-flight.** Research state — full detail + numbers in
  `docs/research_summary.md` (the dated record; the per-phase E1/E2/P3/c2b receipts were
  consolidated there and removed 2026-05-30 — git history has the originals):
  - **Intraday-detection kernel (K0→K1a→I-phase, 2026-05-30) — REOPENED (operator-directed).**
    K0: daily entry ~8–11% below the event-day peak (optimistic ceiling). **K1a falsified only
    the *daily selector run hourly*** (≥6×-daily-turnover can't confirm until ~15:00, after the
    fade) — NOT a purpose-built intraday signal. **I1a:** faders carry a clear cross-venue
    intraday exhaustion fingerprint (peak ~16–17 UTC, turnover climax ~4.2–4.6×, upper-wick
    rejection, OI build on bybit). **I1b (make-or-break) = PASS:** scanning ALL intraday
    rate-bursts (incl. non-events, both venues), a PIT-causal signal SEPARATES faders from
    continuers and **survives beta-neutralization** (idiosyncratic, not market-regime beta) —
    `idio` (pump size vs market) ic_neutral −0.28…−0.31, velocity/vol-spike/accel −0.11…−0.16,
    BOTH venues × BOTH eras; wick = noise. Edge is a SELECTION on pump-extremity (extreme-quintile
    beta-neutral short +1.2–1.3% early / +4.4–4.7% recent, gross 48h); shorting all bursts is
    ~breakeven. A NEW extreme-pump-reversal selector (the daily entry is too late). `scripts/i1b_burst_separation.py`.
  - **I2/c/d/f (2026-05-30/31) — DEPLOYABLE-CANDIDATE at a 25% stop (top-short); NOT validated.**
    Extreme-burst short, realistic engine (`i2_burst_backtest.py`, `i2b_burst_fade_confirm.py`). FADE entries
    (giveback 3–20%, momentum down-bars, volume-decline-vs-climax, failed-retest/no-new-high) ALL underperform
    and are early-negative at ≤20% — entry refinements can't fix a POST-entry bull re-pump squeeze; "more fade"
    empirically loses. The lever is **STOP WIDTH**: the TOP-short (burst entry) flips all-weather at **~25%
    (the operator's cap)** — per-trade net45 EARLY +0.13 bybit / +0.39 binance, RECENT +1.34/+0.51; portfolio
    MAR net45 **3.1/2.2** (net15 5.6/4.3), DD 11–13%. (20–22% marginal; 30% similar.) **Verdict: a deployable
    CANDIDATE exists within ≤25% = the extreme-burst top-short at 25%.** Caveats (NOT validated): Stage-B PROXY;
    **back-loaded** (first calendar-third −6%/−2%); 25% is the boundary + a rough adverse hold; mostly STR. **Next
    = engine-grade I3** (true exit-timing/concurrency + bar_extreme_capped fills + FUNDING + risk_model residual,
    stop≤25%; operator-gated).
  - **I2g–I2k FUNDING DE-RISK (2026-05-31) — funding eats ~85% of the edge; MARGINAL candidate survives under
    FAIR accounting → engine-grade I3 to settle (operator-gated; NOT closed).** Funding *mean* looked like a kill
    but was **outlier-distorted** (hourly-funding coins, LRC −16%); **median** trade ≈0. Funding-to-48h portfolio
    was MAR-negative every hold (12h −0.69/−0.23, 24h −0.54/−0.09, 48h −0.91/−0.73) → looked dead. **But that
    over-charged stopped trades** (a stop exits early; ~13% stopped = the worst crowded-short coins). **FAIR
    funding-to-exit (I2k) reopens it:** at **24h/25%**, ret +4.3%/+5.6%, **MAR +0.30 bybit / +0.49 binance**
    (binance all-weather; bybit positive-but-recent-tilted — underwater ~3y then a recent pop). Crowded-short
    FILTER (I2i) didn't help (funding accrues *during* the hold). **Balanced verdict: real signal (I1b), MARGINAL
    + recent-tilted standalone short found after extensive search (weak evidence); verdict swings with funding
    accounting (−0.54→+0.30→+3.08) = at the proxy's resolution limit.** I3 (true exit-timing/concurrency + capped
    fills + funding-to-exit + residual, 24h, stop≤25%) is the tool to settle it — operator-gated coin-flip.
    **The DAILY age+rmom strategy is the robust validated all-weather edge regardless.** Full write-up:
    `docs/intraday_burst_synthesis.md`. Net: fill-timing dead (E1), detection-timing dead (K1a), standalone
    intraday short = marginal/unvalidated (I2k).
  - **CV1 (cross-venue, 2026-05-30):** the bybit≫binance gap is **BREADTH + universe
    composition, NOT a weaker per-trade edge** — matched (same coin/day) events corr 0.89,
    binance ≈ bybit; per-trade net near-identical (median +0.34%/+0.27%). binance fires ~½ the
    events + its venue-unique coins are weak marginals (less liquid, weaker spike). Edge is
    venue-general on shared names → reassuring for robustness. `scripts/cv1_cross_venue_decomposition.py`.
  - **RD1 (recent decay, 2026-05-30):** the recent per-trade mean decay (both venues) is
    **squeeze-driven** — recent losers are stop-outs on coins pumping *against* a weak market
    (idiosyncratic strength). The **rmom gate fixes it**: cuts ~75% of recent stop-out losers
    (bybit 81→19, binance 57→14), recent mean +0.08%→+0.39% / +0.02%→+0.35%. Explains WHY the
    rmom gate works (squeeze filter) + strengthens the case to forward-demo it.
    `scripts/rd1_recent_decay_rmom.py`.
  - **E1+E1b — execution is a non-lever:** fade-confirmation adds no robust cross-venue premium
    over immediate entry → selection-dominant; E3 (sniper) dropped.
  - **E2/E2b/c/d — the age gate (lead):** `--liquidity-migration-pit-age-days-min≈300` (drop
    names younger than ~300d) roughly doubles cross-venue MAR and fixes the recent weak third;
    robust to threshold/regime/cost/stop-fill — **Tier-2 demo-candidate, in-sample.** Deploy-ready
    (simple PIT feature). Mechanism: young-name shorts are systematic losers (fresh listings squeeze).
  - **P3b — residual-momentum gate:** built + integrated (engine config
    `liquidity_migration_residual_momentum_max`, default-inactive), r1_robustness **DEMO-ELIGIBLE**;
    Tier-3 residual binance-certified, bybit recent-only (not a clean cross-venue cert). Stronger
    than the age gate but its signal must be live-wired before a faithful forward demo.
  - **Continuous architecture (c2b) — C0 NOT justified:** the edge is regime-conditional (recent
    alt-bear only), not all-weather; the robust result is the discrete age gate.
- **Open actions (operator's call — NOT autonomous; profile change is a hard line):** (a)
  forward-demo the **age gate** (deploy-ready) and/or the **residual-momentum gate** (live-wire the
  signal first — `docs/forward_demo_readiness.md`); (b) the deployed demo runs `max_active=3` vs the
  research-validated `max_active=12` (materially lower worst-day + DD) — consider moving it +
  `risk_equal` sizing. Numbers: `docs/research_summary.md`.

## Engine defaults (current)

- **Stop fill: `bar_extreme_capped` (10% cap)** — realistic bad-case (caps adverse slippage
  at 10% beyond the trigger). `stop` (optimistic) and `bar_extreme` (worst-case) remain
  selectable via `--stop-fill-mode`.
- **Cost:** 100% taker; 15 bps base round-trip; sweeps default to ×3 = 45 bps (conservative).
- **Full-PIT universe required** (engine aborts on coverage gaps); the PIT gate is scoped to
  each symbol's traded span `[first_kline, last_kline]` (pre-listing/post-delisting empty
  phantoms excluded; mid-history gaps still caught).
- **Universe sourcing (clarification — the `rank_end: 120` in `configs/volume_alpha.default.yaml`
  is NOT the trading universe):** that 120-rank `universe:` block is a *current-turnover snapshot*
  setting read ONLY by `discover-universe` (a live `get_tickers()` snapshot — survivorship-biased
  by construction, benchmark/scouting only). The actual paths bypass it: the `volume-events`
  backtest reads zero of it — it ranks within the full-PIT root on PIT daily liquidity ranks and
  trades the strategy's `rank_min..rank_max` band; the live demo/paper run match-the-backtest mode
  (`UNIVERSE_RANK_END=0 / UNIVERSE_MAX_SYMBOLS=0` → the full ~750-perp universe). The pre-2026-05-24
  demo did run a narrow current-universe (~220–400 by ticker turnover) — that was a real
  current-universe bias and caused the DRIFTUSDT demo≠backtest divergence; the match-the-backtest
  switch fixed it. So "the old narrow-universe demo was biased" is correct *for that legacy path*;
  the current backtest + live demo are not on the 120.

## Decision rules currently binding — three-tier, demo-arbiter

Principle: permissive where being wrong is free (backtest→demo is paper), strict where it
costs real money. Forward demo/paper is the arbiter. MAR-primary (Return/Drawdown), Sharpe
secondary.

### Tier 1 — Investigation — unchanged
- MAR Δ > 0 on majority venues (2/2 OR 1/2 with other ≥ −0.5 MAR)
- No return sign-flip vs control; ≥30 Bybit / ≥20 Binance trades
- Falsifier: MAR Δ ≤ −1.0 either venue OR return negative OR DD > 70% OR <10 trades/sub-period

### Tier 2 — Demo-candidate (→ forward demo) — LOOSENED
- Return positive on **both** venues (direction guard)
- **Pooled** MAR Δ > +0.1 (mean of the two venue MAR deltas)
- Neither venue worse than MAR Δ ≥ −0.5
- ≥30 Bybit / ≥20 Binance trades total
- Fragility diagnostics (bootstrap p5, LOO, sign-consistency, residual Sharpe) REPORTED,
  non-blocking — set demo order, not eligibility

### Tier 3 — Real-money (demo → mainnet) — STRICT, not loosened
- Forward-demo OOS pass (no internal pre-2023 OOS root exists — pristine OOS = the forward
  demo/paper ledgers, per `docs/data_roots.md`): MAR > 0 both venues over the forward window;
  DD < 50%; sign-consistent
- ≥30 days forward demo + daily paper-shadow reconciliation
- Block-bootstrap pooled MAR-Δ p5 ≥ 0 (seed=0, block=3mo, n=5000)
- Residual Sharpe ≥ +0.3 (factor-model residual; foundation built + validated —
  `liquidity_migration/risk_model.py` `decompose_strategy_pnl`, see
  `docs/preregistration/r4-risk-model-verdict.md`)
- Stress pass + capacity ≥ 10× deployment size

The forward demo (fresh data can't be overfit) is both the multiple-testing arbiter and the
OOS surface — uncapped. `scripts/r1_robustness.py` emits the Tier-2 verdict + fragility from
per-cell ledgers; `scripts/apply_decision_rule.py` is the legacy strict (Sharpe) bar only.

## What's broken

Nothing known. Pre-push gate clean: `.venv/bin/python -m ruff check liquidity_migration tests`
+ `.venv/bin/python -m pytest -q` both pass.

**Fixed 2026-05-30 — coverage_gap false health alert + overhaul audit.** The
`drop_all_4` promotion set `universe_rank_max=99999` (disable sentinel); the demo
health diagnostic computed `required_prior7_rank = universe_rank_max +
rank_improvement_min = 100149` and reported `coverage_gap≈99589`, so the
`demo-health` watchdog paged "universe coverage gap blocks signal generation"
(with an impossible "raise UNIVERSE_RANK_END" action) on a healthy demo. Fix:
`_universe_rank_max_is_binding` treats `rank_max<=0` or `>=10000` as unbounded
(`event_demo.py`) → `coverage_gap=0`; the validator now rejects a truncated
universe for an unbounded-band profile with a clear match-the-backtest message.
Watchdog (`scripts/check_demo_entry_health.py`) no longer pages on a few
non-converting candidates (floor `--zero-entry-candidate-floor`, default 5) — the
"1 candidate" page was noise. Also from the audit: reconcile now reports
`exit_price_gap_bps=None` (not a false 0.0 "perfect") when Bybit omits a closure
price (`reconciliation.py`); `PrivateStateCache.snapshot()` builds row copies
outside the lock (`ws_state_cache.py`). Verified-NOT-bugs (false positives):
the "stale-pending-entry blocks reentry" claim (no trade row is written for an
unfilled demo entry) and three "look-ahead" feature findings (trailing windows on
already-closed bars; also disabled by default). **Post-overhaul ledger reset is an
operator step** — `scripts/reset_demo_paper_ledgers.sh` (archive+wipe the four
roots' trade/order/cycle ledgers; keeps klines) + runbook in
`docs/event_demo_daemon.md`. Deploy = push to main → CI restarts the daemons.

**Fixed 2026-05-30 — PIT gate / reconcile plumbing** (was: backtest↔paper showed
spurious `pit_membership_fail`/`paper-only`). Root cause: PIT membership was keyed
on the signal *stamp* date (D+1, daily-close signals fire at 00:00 of the next day)
instead of the *trading* day; the archive only publishes the trading day, so fresh
signals never validated. Fix keys membership on `date(ts_ms-1ms)`
(`volume_events_features._attach_event_archive_membership`), proven on the real
Bybit manifest (HEMIUSDT et al. now pass). Plus: `pit_coverage.py` staleness check,
`download-data` coverage warning + `--refresh-manifest`, `volume-events
--pit-membership strict|current-universe`, richer reject diagnostics, and a
bash-3.2-safe `volume_events_cell.sh`. **One-command reconcile:
`bash scripts/reconcile.sh`** (skill `pit-reconcile`, design `docs/pit_gate.md`).
Op note: the 16 GB research box can't run a full `bybit_full_pit` cell (~23 GB).

## Helpers (when you need them)

- **Demo-forward reconcile (one command):** `bash scripts/reconcile.sh` — pull VPS
  ledgers → refresh manifest → coverage check → backtest → `reconcile-all` →
  summary. `--dry-run` to preview. Skill: `pit-reconcile`; design: `docs/pit_gate.md`.
- **CLI baseline wrapper:** `scripts/volume_events_cell.sh --cell-id X --overrides 'KEY=VAL,…'`
  fills the 30+ baseline flags (now bash-3.2-safe on macOS; `DRY_RUN=1` to preview).
- **Decision-rule analyzer:** `scripts/apply_decision_rule.py SUMMARY.csv --control 00_baseline`.
- **Tier-2 verdict + fragility:** `scripts/r1_robustness.py --sweep-tag <TAG>`.
- **Skill `research-phase-runner`** (auto-loads) — per-phase run/verdict workflow.
- **MCP tools** on `liqmig-research`: `current_state`, `data_roots`, `list_reports`,
  `parse_report`, `audit_run_artifacts`, `apply_decision_rule`.
- **Full-PIT op note:** one `volume-events` cell peaks ~23 GB → run full-PIT sweeps at
  `SWEEP_MAX_WORKERS=1 POLARS_MAX_THREADS=8` (over-parallelizing OOMs the box); clear
  `<root>/.locks/*.lock` after any OOM/kill.

## Non-negotiables (every session)

1. Pre-push gate (`ruff` + `pytest`) before every `git push`.
2. Never `REAL_MONEY=true`. Demo + paper only.
3. Never commit or push without operator confirmation.
4. Never modify `docs/backtesting_errors_we_never_repeat.md`,
   `docs/parameter_pre_registration.md`, or `configs/volume_alpha.default.yaml` without
   operator instruction.
5. The three-tier decision structure is pre-committed — no further loosening to rescue a
   specific cell; the Tier-3 real-money gate is NOT loosened.
6. MAR-primary, Sharpe-secondary is pre-committed.
7. Strategy stays at the frozen promoted profile until the Tier-3 gate passes AND ≥30 days
   forward demo evidence accumulates.

## How to update this file

Keep it short (live/operational state + decision rules). Research results go in
`docs/research_summary.md`, not here. Keep under ~120 lines.
