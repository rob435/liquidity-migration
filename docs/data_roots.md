# Data Roots

Current as of 2026-05-24 (rewritten for the full-PIT rebuild — see
[docs/full_pit_rebuild_and_punchlist.md](full_pit_rebuild_and_punchlist.md)).

## Per-venue full-PIT working datasets

Two clean per-venue roots replace the previous three-root patchwork. There is
no internal OOS/IS split: each per-venue dataset is the working surface and the
two venues serve as side-by-side validation of any signal claim.

```text
~/SHARED_DATA/bybit_full_pit       Bybit USDT linear perpetuals, ~2021-01..today
                                   source: public.bybit.com/trading archive
                                   + Bybit v5 kline REST (manifest-gated)
                                   + Bybit v5 REST funding/OI/mark/index/premium
                                   (signed_flow build removed 2026-05-25 —
                                    validated as not-an-edge across 5+ tests)

~/SHARED_DATA/binance_full_pit     Binance USD-M perpetuals, ~2019-09..today
                                   source: data.binance.vision monthly archives
                                   + Binance fapi REST funding/OI/mark/index/premium
                                   + taker_flow_1h
```

Both roots are perpetuals-only by construction. The build scripts assert
USDT-quoted symbols and fail loudly if any non-USDT symbol slips through.

Rebuild on any machine (idempotent, resumable, takes ~17-31 hours unattended):

```bash
bash scripts/build_full_pit_roots.sh        # full pipeline
# Or the per-venue stages individually:
bash scripts/archive_pre_rebuild_reports.sh
bash scripts/build_full_pit_bybit.sh
bash scripts/build_full_pit_binance.sh
bash scripts/verify_full_pit_rebuild.sh
```

These roots are **not committed** (data, not code).

## Pristine out-of-sample = forward only

Since both per-venue roots span their full available histories, there is no
clean internal OOS window left in either venue. **Pristine OOS henceforth is
the forward demo + paper ledgers, ticking from 2026-05-22.**

When a candidate parameter set is promoted, the forward ledgers accumulate
clean OOS PnL that no backtest sweep can touch. Cite forward returns as the
OOS evidence; cite either per-venue root as working-dataset evidence.

## Live demo + paper roots

The live Bybit demo runner intentionally uses a separate operational root:

```text
data/bybit-demo-event
```

Do not point the live demo order/trade ledgers at the full-PIT research root.
The demo root contains forward kline cache, order ledgers, trade ledgers, cycle
reports, and risk-watchdog reports.

The parallel paper (dry-run) runner uses its own separate root:

```text
data/bybit-paper-event
```

It shadows the demo runner — same strategy profile, universe, and cadence — but
submits no orders and records idealized fills at the signal price. Comparing the
paper and demo ledgers measures demo-vs-paper execution slippage; the
`reconcile-paper-demo` CLI command does that comparison for the short sleeve;
`reconcile-long-paper-demo` does the same for the long sleeve.

## Retired Roots

The following roots are superseded by the rebuild and are scheduled for
deletion after verification gates pass:

```text
~/SHARED_DATA/bybit_fullpit_1h     2023-05..2026-05 — superseded by bybit_full_pit
~/SHARED_DATA/bybit_oos_pre2023    2021-01..2023-05 — superseded by bybit_full_pit
~/SHARED_DATA/binance_oos_pit      2020-01..2023-04 — superseded by binance_full_pit
```

Do not cite these for new research after the rebuild. Reports/markers from
them are archived under `~/SHARED_DATA/archive/` for audit trail.

Do not use ad hoc current-universe or temporary recent roots for promotion
evidence. Current-universe research is biased by construction unless membership
is point-in-time. A live `exchangeInfo` snapshot is never an acceptable
cross-venue PIT source — see `docs/backtesting_errors_we_never_repeat.md`.
