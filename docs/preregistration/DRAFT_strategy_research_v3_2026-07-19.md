# DRAFT — Strategy Research V3 theses with execution plans

Status: exploratory research program, Lane-1/Lane-2 model. Nothing here
changes the deployed runtime, profile, sizing, or the active 90-day epoch.
Mechanism-first theses (owner-derived: destroyed by tails and funding) carry
low mining risk; the parts that still need out-of-sample evidence are the
tuned parameter values and the net effect of skipped trades. Those are judged
by rolling forward scoring, where the "freeze" is simply the git commit date
of a prototype's config preceding the scored day.

Working rules (light):

- Tune and iterate freely on the spent discovery surface. Label everything
  exploratory.
- Judgment comes from data the rule's commit predates: forward accrual
  post-2026-07-06 as it arrives. The `[2025-01-01, 2026-07-06)` label-level
  V2 holdout stays unread unless the owner deliberately spends it; note the
  deployed-profile *equity renders* already covered that period, so it is
  only unseen at the candidate/label level.
- Era-stability check on every result (early half vs late half) — a pooled
  number that hides decay is a wrong answer, not a pass.
- Report effect sizes with the modeled cost of the exact trade shape next to
  them; no inherited thresholds.

## Big-PC runbook (common to all theses)

- Interpreter: `.venv\Scripts\python.exe` (Python 3.13.6). Do not run
  `scripts/dev.sh check` on Windows (known platform-stub mypy failure);
  use whole-repo Ruff plus focused pytest for the touched research scripts.
- Data root: `C:\Users\user\SHARED_DATA\bybit_full_pit`. Verify manifest,
  kline, and funding coverage for the exact window before trusting a run;
  the directory name proves nothing.
- V2 per-trade ledger (primary input for T-B/T-C):
  `reports/strategy-overhaul-v2/diagnostic-epoch-2026-07-17/phase3-analysis/barebones_ledger.parquet`
  (SHA-256 `368a7c04640dd362179d4c00897948d036ce38dc6136da12eedd47b4b6c64ddd`;
  verify before use, together with `manifest.json` canonical payload hash
  `0a14862522af6e37ea05facbb47f9f4564e6f298ccb4a2d3559f5a79b0f06d9d`).
  Row identity: `net = gross + cost + funding`; recheck that identity on load.
- All outputs under `reports/strategy-research-v3/<thesis>/<date>/` with a
  small `manifest.json` (inputs, hashes, code commit, parameter grid).
- Never point anything at operational/demo account roots; research roots
  only. No VPS interaction.
- New code goes in `scripts/research_v3/` as read-only analysis entry
  points; do not modify the deployed runtime modules for any of this.

---

## T-A. Regime-gate ablation (owner input: larger sample)

**Thesis.** The BTC uptrend gate does not pay for its sample cost: gated-off
entries are not net-negative after costs, and the gate's tail protection is
smaller than its opportunity cost. Both an economics read and a tail read are
required; removal must not win on mean while losing on the 2024-08-06-style
joint-loss days.

**Execution plan.**
1. Add a research-only override to the standard equity runner chain
   (`scripts/equity_curves.py` → `scripts/continuous_deployed_equity_refresh.py`):
   a `--research-disable-btc-gate` flag that bypasses the uptrend regime
   check (`liquidity_migration/continuous_profile.py` /
   `continuous_regime.ACTIVE_BTCVOL_REGIME`) in the research render only.
   Guard the flag so it cannot reach operational entry points.
2. Run the standard CONTINUOUS render twice on the full-PIT root over the
   full available window: baseline (gate on) and ablation (gate off).
   Identical cost model, identical components.
3. Produce a paired diff table: total/annualized return, max drawdown, worst
   day, entry count, per-entry net, and — the tail arm — drawdown and
   negative-contribution on the dates where both sleeves historically lost
   together (the 156 common-loss dates listed in the V2 diagnostics).
4. Split every metric into early/late halves (era stability).
5. Deliverable: one summary table answering "what does the gate buy, and
   what does it cost, per era and in the tail." If gate-off survives both
   arms, promote the ablation config into the forward rolling ledger as a
   prototype; the live profile changes only through the normal post-epoch
   deploy flow.

## T-B. Funding-floor entry/exit economics (CONTINUOUS)

**Thesis.** Requiring TP distance to clear `modeled costs + known funding
floor` at entry, and exiting when realized+projected funding consumes a
declared fraction of TP distance, improves per-trade net. The floor uses only
PIT-known values: current next-settlement rate × funding intervals in the
expected hold. Structure is an accounting identity; the open questions are
the net effect of skipped trades and the threshold values.

**Execution plan.**
1. Build a per-symbol funding panel from the PIT root's funding files:
   settlement timestamps, rates, and per-symbol funding interval (1h/4h/8h —
   derive from observed settlement spacing, do not assume 8h).
2. Join to `barebones_ledger.parquet` per trade: funding rate known at entry,
   interval count over the realized hold, realized funding paid (already a
   ledger column — cross-check the join against it; mismatches are a data
   bug, stop and explain before proceeding).
3. Compute the entry floor per trade and the counterfactual filter: which
   trades fail `TP_distance > costs + floor × multiple` for a declared grid
   of multiples (e.g. 1.0, 1.25, 1.5) — enumerate the grid in the output
   manifest, report all cells, no cherry-picking.
4. Re-run the fixed-capital portfolio recurrence on the filtered ledgers
   (pure post-processing; no account replay needed for this pass). Report
   gross forgone by skipped trades vs funding+cost saved — this is the
   salience-bias check: the mechanism guarantees the cost side only.
5. Simulate the drain-exit rule on the surviving trades using the funding
   panel along each hold (exit when cumulative funding > declared fraction
   of TP distance; grid the fraction, report all cells).
6. Deliverable: grid table of net return / drawdown / trade count vs
   baseline, split by era. Winners become prototype configs for the forward
   rolling ledger.

## T-C. Pump-deceleration entry timing (CONTINUOUS)

**Thesis.** Fade entries during accelerating pumps carry the deepest adverse
excursions (V2: MAE −13.4% vs MFE +11.4%; live 2026-07-19 stop-out cluster).
Requiring deceleration before entry cuts stop-outs and MAE by more than the
missed-entry cost.

**Execution plan.**
1. From PIT klines strictly before each ledger entry timestamp, compute a
   small declared feature set: 1h return, momentum-of-momentum (Δ of 1h
   return), and time-since-local-high. Enumerate the exact definitions in
   the output manifest.
2. Bucket the existing ledger's MAE, stop-exit frequency, and net by
   acceleration state at entry. This alone is the diagnostic deliverable:
   does adverse path concentrate where the mechanism says?
3. Apply counterfactual entry-delay rules (skip vs delay-until-deceleration;
   for delayed entries re-price the entry from the kline at the delayed
   time, keeping the same exit logic) on a declared grid.
4. Re-run the portfolio recurrence per rule; report net, MAE distribution
   shift, stop-out rate, and forgone winners, split by era.
5. Deliverable: bucketed diagnostic + counterfactual grid. Winners become
   forward-ledger prototypes.

## T-D. Funding forecast beyond the next interval (CONTINUOUS)

**Thesis.** Cumulative funding over a 24–72h hold is predictable from
PIT-known inputs better than the T-B constant floor, especially on
crazy-funding symbols where the floor is most wrong (owner-reported trade:
TP hit, net loss from funding).

**Execution plan.**
1. Reuse the T-B funding panel. Targets: realized cumulative funding over
   24h/48h/72h horizons from each settlement time.
2. Baseline: persistence (current rate held constant over the horizon).
   Candidates (declared list): EWMA persistence-with-decay; mean-reversion
   from extremes; regression on premium-index trend, basis, and
   open-interest change where OI files exist in the root.
3. Walk-forward development inside the spent window (train early, score
   later) — this is development, not confirmation; report MAE/RMSE and the
   tail-quantile error (the crazy-funding cases are the point; average error
   alone is a wrong metric).
4. Stage 2 (only if a model clearly beats persistence): substitute the
   model for the T-B floor and re-run the T-B grid; compare cells.
5. Deliverable: forecast scoreboard vs persistence + Stage-2 economics
   delta. The committed model config then rides the forward ledger like any
   other prototype.

---

## Forward rolling ledger (Lane 2, shared by all winners)

One scorer, run daily or in batches: replays each new UTC day of PIT data
against every committed prototype config, appends one row per prototype per
day (net delta vs unmodified baseline, trades affected, funding saved, gross
forgone), and keeps a cumulative scoreboard. A prototype's evidence is the
run of days its commit predates. Promotion to the live profile is a separate
owner decision through the normal deploy flow, summarized in five lines:
claim, config commit, forward record, decision, date.
