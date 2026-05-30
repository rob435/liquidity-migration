# Data Roots

Canonical index of which data root to use (research full-PIT vs. live demo/paper
vs. forward OOS). Whether a root is currently built/present is live state — see STATE.md.

## The roots are data, not code

The per-venue full-PIT roots (`~/SHARED_DATA/bybit_full_pit`,
`~/SHARED_DATA/binance_full_pit`) are data, not code — not committed. If a root is
ever lost, the rebuild scripts below are the recovery path. `binance_full_pit_strategy`
(derived long-sleeve reports + the canonical funding dataset) is a separate root.

## Per-venue full-PIT working datasets (intended state)

Two clean per-venue roots are the working surface — no internal OOS/IS
split. Side-by-side venue comparison is the validation: agreement = robust
signal, disagreement = regime/microstructure artefact.

```text
~/SHARED_DATA/bybit_full_pit       Bybit USDT linear perpetuals, ~2021-01..today
                                   source: public.bybit.com/trading archive
                                   + Bybit v5 kline REST (manifest-gated)
                                   + Bybit v5 REST funding/OI/mark/index/premium

~/SHARED_DATA/binance_full_pit     Binance USD-M perpetuals, ~2019-09..today
                                   source: data.binance.vision monthly archives
                                   + Binance fapi REST funding/OI/mark/index/premium
                                   + taker_flow_1h
```

Both roots are perpetuals-only by construction. The build scripts assert
USDT-quoted symbols and fail loudly if any non-USDT symbol slips through.

Rebuild on any machine (idempotent, resumable):

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

When the per-venue roots span their full available histories there is no
clean internal OOS window left in either venue. **Pristine OOS henceforth is
the forward demo + paper ledgers, ticking from 2026-05-22.**

When a candidate parameter set is promoted, the forward ledgers accumulate
clean OOS PnL that no backtest sweep can touch. Cite forward returns as the
OOS evidence; cite either per-venue root as working-dataset evidence.

## Live demo + paper roots

The live Bybit demo runner intentionally uses a separate operational root,
**on the VPS** (these are not local on the research machine):

```text
/opt/liquidity-migration/data/bybit-demo-event
/opt/liquidity-migration/data/bybit-long-demo-event
```

The VPS ledgers were unaffected by the 2026-05-27 research-root deletion.
Do not point the live demo order/trade ledgers at any research root. Each
demo root contains its forward kline cache, order ledgers, trade ledgers,
cycle reports, and risk-watchdog reports.

The parallel paper (dry-run) runner uses its own separate root
(`data/bybit-paper-event`). It shadows the demo runner — same strategy
profile, universe, and cadence — but submits no orders and records
idealized fills at the signal price. Comparing the paper and demo ledgers
measures demo-vs-paper execution slippage. Run `bash scripts/reconcile.sh`
(skill: `pit-reconcile`) for the full demo↔paper↔backtest↔Bybit reconcile — it is
the only reconcile entrypoint; do not hand-assemble the `reconcile-*` calls.

Do not use ad hoc current-universe or temporary recent roots for promotion
evidence. Current-universe research is biased by construction unless
membership is point-in-time. A live `exchangeInfo` snapshot is never an
acceptable cross-venue PIT source — see
`docs/backtesting_errors_we_never_repeat.md`.
