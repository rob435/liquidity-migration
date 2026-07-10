# Data Roots

Canonical index of which data root to use (research full-PIT vs. live demo/paper
vs. forward OOS). Whether a root is currently built/present is live state — see STATE.md.

## The roots are data, not code

The per-venue full-PIT roots (`~/SHARED_DATA/bybit_full_pit`,
`~/SHARED_DATA/binance_full_pit`) are data, not code — not committed. If a root is
ever lost, the rebuild scripts below are the recovery path. The canonical Binance
funding dataset is `binance_full_pit/binance_usdm_funding`; verify its current
window coverage from the run/audit rather than a historical rebuild claim. The old
`binance_full_pit_strategy` side-root no longer exists on this box.

## Per-venue full-PIT working datasets (intended state)

The two per-venue roots are storage surfaces, not statistical splits. A study
may reserve temporal holdouts, use walk-forward evaluation, or use only the
target venue. Cross-venue comparison is valuable when portability is part of
the claim, but agreement between correlated crypto venues is not independent
OOS proof and disagreement is evidence to explain, not automatically an
artefact.

```text
~/SHARED_DATA/bybit_full_pit       Bybit USDT linear perpetuals, ~2021-01..today
                                   source: public.bybit.com/trading archive
                                   + Bybit v5 kline REST (manifest-gated 1h/5m)
                                   + Bybit v5 REST funding/OI/mark/index/premium

~/SHARED_DATA/binance_full_pit     Binance USD-M perpetuals, ~2019-09..today
                                   source: data.binance.vision monthly/daily archives
                                   (canonical 1h/5m klines)
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

## Evaluation surfaces and data exposure

A root spanning full available history does not mean every row must be used for
design. Historical windows can be held out prospectively for a genuinely new
claim. For the current strategies, much of both histories has already influenced
research, so new paper/demo epochs are often the cleanest remaining prospective
surface.

Forward data stays untouched only until it is inspected or used to change the
profile, threshold, stopping decision, or clock. Preserve immutable epochs and
all reset/change points. A new clock does not erase the evidentiary exposure of
earlier ledgers. See `docs/governance.md`.

## Live demo + paper roots

The live Bybit demo runner intentionally uses a separate operational root,
**on the VPS** (these are not local on the research machine):

```text
/opt/liquidity-migration/data/bybit-demo-event            # ws_risk engine root: heartbeat cycles + reports
/opt/liquidity-migration/data/bybit-paper-event           # unused paper root, no writer
/opt/liquidity-migration/data/bybit-long-demo-event
/opt/liquidity-migration/data/bybit-long-paper-event
/opt/liquidity-migration/data/bybit-continuous-demo-event
/opt/liquidity-migration/data/bybit-continuous-paper-event
/opt/liquidity-migration/data/bybit-continuous-hedge-event
```

Which sleeves are live at any moment is `deploy/sleeves.env` + STATE.md, not
this file; every root keeps its ledgers regardless of toggle state because
`ws_risk` reads all configured roots.

VPS ledger history restarted from a clean slate at the 2026-06-09 full rebuild
(all prior demo/paper history lost — see STATE.md). The research roots and VPS
roots remain fully independent.
Do not point the live demo order/trade ledgers at any research root. Each
demo root contains its forward kline cache, order ledgers, trade ledgers,
cycle reports, and risk-watchdog reports.

Each sleeve has a paper (dry-run) shadow on its own root (long:
`data/bybit-long-paper-event`, continuous: `data/bybit-continuous-paper-event`)
— same profile/universe/cadence, no orders, idealized fills at signal price.
Comparing the paper and demo ledgers measures demo-vs-paper execution slippage.
Run `bash scripts/reconcile.sh` (skill: `pit-reconcile`) — the single reconcile
entrypoint (full demo↔backtest↔paper by default; `--quick` for the fast
demo↔paper-only check). Do not hand-assemble the `reconcile-*` calls.

Do not use ad hoc current-universe or temporary recent roots for a historical
universe claim. They remain useful for explicitly scoped diagnostics. A live
`exchangeInfo` snapshot is not a historical membership source; see
`docs/pit_gate.md` and `docs/governance.md`.
