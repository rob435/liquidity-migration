# Pre-registration: P10 Stage-1 — taker-flow squeeze-proxy scout (event-anchored)

**Date:** 2026-06-12 (registered BEFORE the run). **Label:** `exploratory` Stage-1
information scout — gates a Stage-2 DESIGN, never deployment, never sizing.
**Authority:** the standing operator /goal autonomous-research directive
(2026-06-12) + charter Wave-3 P10. **Independent priors (assembled before any
in-repo data was touched):** `docs/research_notes_external_priors_2026-06-12.md`.

## What's being tested

Whether the TAKER-FLOW composition of a pop event (the un-tested half of the
liquidation/squeeze proxy — the OI half is closed: Stage-1 PASS, tilt NULL,
down-size NULL) carries cross-venue information about the fade quality of
winner_base continuous-book entries.

## Premise

The fade is a squeeze/forced-flow phenomenon. The OI scout proved event-level
positioning information exists (rising-OI pops fade better, IC ≈ +0.08 both
venues). Taker flow is the OTHER observable leg of forced flow, it is
survivorship-FREE on bybit (tick archive serves delisted symbols — repairing the
bybit arm that was survivor-only for OI), and it does NOT inherit the
OI-feed-lag-during-cascades caveat (arXiv 2310.14973). External priors conflict
on the SIGN (crowded-pump reversion vs CVD-divergence reversion — priors note
§2), so the primary is TWO-SIDED with cross-venue sign agreement as the guard.

## Data (new layer + existing)

- bybit: `bybit_full_pit/taker_flow_5m` — 5-min signed taker flow (quote
  notional, buy/sell, trade counts) aggregated from side-flagged
  public.bybit.com tick CSVs; event-anchored coverage [t0−50h, t0] per event;
  absence-auditable `_manifest.json`. Builder:
  `scripts/bybit_taker_flow_backfill.py`. Live-path equivalent: WS trade-stream
  aggregation (the liquidation-collector pattern) — noted, not built here.
- binance: `binance_full_pit/binance_usdm_metrics_5m`
  (`sum_taker_long_short_vol_ratio`, 5-min, survivorship-free). Live-path
  equivalent: the REST taker endpoint already feeding
  `binance_usdm_taker_flow_1h` (30d window) — PIT research mirror is Vision.

## Construction (fixed a priori)

- **Events:** the frozen winner_base component ledgers (same loader as the OI
  scout: COMP_PRIORITY = turn4p5, turn3p3, turn4p3, age210tp14; dedup on
  (symbol, entry_signal_ts_ms)). Outcome = ledger `net_return`.
- **Bucket imbalance:** bybit `imb = (buy_quote − sell_quote)/(buy_quote +
  sell_quote)` (zero-total buckets null); binance `imb = (r − 1)/(r + 1)`,
  r = sum_taker_long_short_vol_ratio (missing rows null).
- **Primary feature `flow_support_6h`:** unweighted mean of bucket imbalances
  over buckets in (t0−6h, t0−5m]; null unless ≥48 buckets non-null. Identical
  construction both venues (equal-construction > per-venue optimality;
  declared).
- **Confirmation copies:** `flow_support_24h` ((t0−24h, t0−5m], ≥192 buckets);
  `flow_support_6h_lag1` ((t0−7h, t0−1h−5m]) — latency honesty.
- **Causality (pre-run amendment, before any outcome computation):** the
  binance metrics `create_time` stamp convention (interval start vs end) is
  unverified; to be strictly causal under EITHER convention, every window ends
  at t0−5m (no bucket may extend past t0) on BOTH venues. Cost: ≤1 bucket of
  freshness on a ≥71-bucket feature. Live availability of trade-tape
  aggregates at t0 is real-time; the lag-1 falsifier guards residual
  publication latency.

## Primary gate (two-sided, cross-venue — ALL must hold)

1. |Spearman IC(flow_support_6h, net_return)| ≥ 0.08 with p < 0.05 on BOTH
   venues (normal-approx two-sided, same stats code as the OI scout).
2. SAME sign on both venues.
3. Coverage ≥ 60% of events per venue (else the run is reported
   coverage-limited and NOT cited as a verdict either way).

## Falsifiers / confirmations (reported; 4-6 must hold for a clean PASS)

4. `flow_support_24h` IC same sign as 6h on both venues.
5. `flow_support_6h_lag1` IC same sign on both venues.
6. Not-a-trigger-proxy: |spearman(flow_support_6h, score)| < 0.3 per venue.
7. Not-an-OI-proxy (incremental value): on the d_oi_6h-covered subset,
   spearman(flow_support_6h, d_oi_6h) reported, AND the partial IC (flow ranks
   rank-residualized on d_oi_6h ranks, vs net_return) keeps the primary's sign
   on both venues.
8. Per-year ICs reported (recency check — a 2026-only carry is flagged, as in
   the OI scout's bybit arm).
9. Survivor-coverage diagnostic: covered vs uncovered mean net per venue
   (bybit expected near-100% covered — reported as the structural upgrade).

## Secondary (descriptive ONLY, no gate, no variant search)

The 2×2 mean net_return by (sign of flow_support_6h × sign of d_oi_6h) on the
joint-covered subset — mechanism articulation if the primary passes
(new-long-driven vs covering-driven pops). Not a pass/fail object.

## Multiple-testing posture

ONE primary feature, one primary window (6h), 2 venues; 24h/lag-1 are
confirmations, not search; the 2×2 is descriptive. No re-tuned windows, no
alternative weightings, no rank/tanh variants. FAIL retires event-level
taker-flow conditioning of the continuous book on this window (one shot, like
P5/P7/P8). The external-priors note (written first) is the independent-prior
guard against new-dataset multiple-testing creep.

## Conversion path (pre-stated)

PASS → Stage-2 = per-event binary ENTRY VETO of the pre-identified worst class,
judged at full Tier-2 + ±5% gross guard under a FRESH receipt. Explicitly NOT a
continuous size tilt (family closed by Stage-2-OI + P8). FAIL → family retired;
the data layer remains (P11/P12 value stands).

## What would make a PASS disappear

Venue sign disagreement (the historical mirage mode); lag falsifier failure
(latency artifact); flow_support proxying the trigger score or ΔOI; a
2026-carried pooled IC; coverage below 60% (selection on data presence).

## Run

```bash
PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe scripts/continuous_taker_flow_scout.py [--coverage-only]
```

Artifacts: `C:/Users/user/SHARED_DATA/continuous_taker_flow_scout_2026-06-12/`
(report.json + per-venue event parquets).

## Post-run results

(fill in after run)

## Verdict

(fill in after run)
