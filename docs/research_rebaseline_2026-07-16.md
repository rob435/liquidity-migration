# Research Rebaseline — 2026-07-16

This is a point-in-time baseline of the repository after the account-owner and
operational architecture changes. It is not an operational receipt, a research
result, or a deployment authorization. Runtime facts remain in `STATE.md` and
research claims remain subject to `docs/governance.md`.

## Identity and health

The code baseline was taken from clean, synchronized `main` at
`c8251fffd4777a53c153a7798c4c722563b1d93e` before new overhaul work began.
The authenticated operational receipt recorded during the same session is the
documentation-only commit `88261089cf03366886f9beac41b26eff2bebede2`, now the
base of this branch and `origin/main`.

| Check | Baseline fact |
| --- | --- |
| Selected Python | Repository `.venv`, Python 3.13.5 |
| Dependency contract | Exact match to all 26 entries in `requirements.lock` after local re-sync |
| Test discovery | 1,911 tests collected |
| Project skills | Eight canonical files, byte-matched to the Claude mirror |
| Graphify | Present and readable; derived navigation only |
| Tracked surface | 306 files; 92 package modules, 108 test modules, 30 Python/shell scripts |

`STATE.md` identifies `c8251fffd4777a53c153a7798c4c722563b1d93e`
as the installed implementation. The current remote branch is ahead only by
the documentation receipt in `8826108`; its implemented code matches the
installed commit at this baseline. The new diagnostic work on this branch is
not deployed, and its correctness must remain separate from operational state.

## Current architecture

The current system already has the control plane the retired overhaul tried to
recreate:

```text
strategy event -> component target -> durable inbox -> account service
               -> account kernel -> risk decision -> aggregate order command
               -> ACK/fill/status -> canonical account journal

sequence-aware L2 -> exact decision-book context ----^
```

- Strategy producers own signal calculation and absolute component targets.
- The account owner owns cross-sleeve aggregation, account risk, venue commands,
  private order/execution observations, reconciliation, and accounting.
- The hash-verified account journal is the lifecycle and accounting authority.
- The market recorder keeps a sequence-aware live book and persists an exact
  decision context even when bulk raw-market persistence is disabled.
- Mutable Parquet order/trade/cycle views are projections, not authority.
- PIT manifests and historical datasets answer population questions; they do
  not replace the execution journal or decision books.

This means a new research platform, artifact registry, supervisor graph, or
parallel execution model would duplicate current ownership. The missing layer
is a small analytical projection over canonical evidence.

## Research and data boundary

No confirmatory experiment is active. The retained CONTINUOUS and LONG results
are limited controls under `docs/research_summary.md`; neither reconstructs the
complete current account/runtime path. Paper remains
`integration_only_uncalibrated`.

The local workspace contains legacy and current Parquet projections but no
canonical demo/paper account-journal transaction tree or decision-book capture
root. A schema-only inventory, without inspecting price, return, or PnL values,
found:

| Local projection | Rows | Columns |
| --- | ---: | ---: |
| CONTINUOUS demo cycles | 2,849 | 107 |
| CONTINUOUS demo order updates | 52,212 | 52 |
| CONTINUOUS demo trades | 21 | 65 |
| CONTINUOUS paper cycles | 2,862 | 107 |
| CONTINUOUS paper order updates | 42 | 49 |
| CONTINUOUS paper trades | 24 | 58 |
| LONG demo cycles | 502 | 72 |
| LONG paper cycles | 532 | 72 |

The 52,212 order-view rows are repeated mutable observations, not 52,212
independent orders or decisions. Their contrast with 21 trade rows is another
reason to derive diagnostics from canonical command IDs, execution IDs, unique
decisions, and waves rather than projection row counts.

## Diagnostic coverage and gaps

| Capability | Current evidence | Gap to close |
| --- | --- | --- |
| Decision-to-fill lineage | Canonical journal links batch, target, risk, command, ACK, fill, status, and PnL | No compact command-grain TCA projection |
| Arrival benchmark | Exact depth-50 decision book with engine/local timestamps and sequence health | Context locator is not yet optimized for analytical lookup |
| Fill economics | Execution price, quantity, observed fee, venue and execution IDs | Maker flag, fee rate, execution type/value, leaves, and fee currency are not retained |
| Latency | Command, local send/ACK, exchange ACK/fill, and local fill receipt clocks | Clock domains need explicit labels and no mixed-clock subtraction |
| Post-fill adverse selection | None at fixed sub-hour horizons when raw persistence is disabled | Add bounded markout snapshots rather than restoring bulk capture |
| Strategy selection funnel | Cycle-level aggregate skip counters and accepted-target metadata | No row-level pre-gate population with first rejection and missingness |
| Path diagnostics | Historical lifecycle code and some trade projections | Signal-time inputs, entry anchors, and future labels need separate keyed outputs |
| Statistical grain | Retired lessons identify unique decision, wave, and calendar block | Enforce these keys in every new diagnostic/report |

## Rebaseline decision

Retain the current account, capture, PIT, and strategy ownership. Add only:

1. a command-grain execution/TCA projection from verified journal plus exact
   decision books;
2. bounded post-fill markout contexts;
3. a row-level pre-gate decision funnel;
4. separate future path labels when a named claim needs them.

The new overhaul must produce a decision-useful table before expanding its
infrastructure. Its plan and artifact budget are in
`docs/strategy_overhaul_v2_plan.md`; metric definitions are in
`docs/trade_diagnostics.md`.
