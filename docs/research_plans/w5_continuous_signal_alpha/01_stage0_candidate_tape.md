# Stage 0 - Candidate Tape And Baseline Reconstruction

## Purpose

Build the audit surface for all later score-entry, entry, exit, sniper, and
sizing work. This stage makes no alpha claim.

The key requirement is to reconstruct not only executed trades, but the full
eligible candidate set at each decision cycle. Without rejected-but-eligible
candidates, score-entry work degenerates into survivorship-biased trade review.

## Inputs

- Full-PIT roots:
  - `~/SHARED_DATA/bybit_full_pit`
  - `~/SHARED_DATA/binance_full_pit`
- Current common window unless refreshed:
  - `2023-04-01 <= signal_ts < 2026-05-01`
- Frozen continuous component definitions:
  - p3, p4p3, p4p5, tp14;
  - frozen ensemble weights;
  - frozen BTC uptrend gate.

## Output Event Tape

One row per candidate per decision cycle:

- venue;
- component;
- symbol;
- decision cycle id;
- `decision_ts`;
- `data_available_ts`;
- `order_submit_ts`;
- candidate rank within component;
- candidate rank after ensemble merge;
- raw composite;
- component flags;
- selected/not selected;
- reason not selected;
- current active positions;
- crowding state;
- cooldown state;
- symbol age;
- liquidity/turnover;
- current score features;
- W4 path-shape features:
  - `pre_6h_return`;
  - `pre_24h_return`;
  - `pre_24h_realized_vol`;
- negative controls:
  - symbol hash bucket;
  - month hash bucket;
  - shuffled-in-fold score.

## Baseline Reconstruction

Rebuild the frozen control from the candidate tape:

- selected entries must match current component ledgers within tight numerical
  tolerance;
- selection counts by component and date must reconcile;
- ensemble ledger must match the frozen control ledger;
- R1 control metrics must match existing W4 control metrics within tolerance.

## Required Artifacts

Recommended artifact root:

`~/SHARED_DATA/w5_continuous_stage0_candidate_tape_YYYY-MM-DD/`

Files:

- `candidate_tape_{venue}.parquet`;
- `selected_entries_{venue}.csv`;
- `baseline_reconstruction_{venue}.csv`;
- `stage0_summary.json`;
- `stage0_summary.md`;
- `pit_gate.json`;
- `config_hashes.json`;
- `code_hash.txt`.

## Falsifier

Stage 0 fails if:

- candidate tape cannot be reconstructed on both venues;
- PIT gate fails;
- selected entries do not reconcile to control;
- timing fields are missing;
- rejected candidates are unavailable.

If Stage 0 fails, do not run score-entry, entry, exit, sniper, or sizing stages.
