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

## Demo + paper operational roots

The repository account-kernel layout uses separate operational roots **on the
VPS** (these are not local research roots). Exact host paths come from
`/etc/liquidity-migration/account-{,paper-}execution.env`; the canonical layout
is:

```text
/opt/liquidity-migration/data/bybit-account-execution       # demo account journal + projections + owner health
/opt/liquidity-migration/data/bybit-account-intents         # atomic demo target-request inbox
/opt/liquidity-migration/data/bybit-account-market-capture  # raw demo-owner L2 capture
/opt/liquidity-migration/data/bybit-account-paper           # paper account journal + projections + owner health
/opt/liquidity-migration/data/bybit-account-paper-intents   # atomic paper target-request inbox
/opt/liquidity-migration/data/bybit-account-paper-market-capture # independent raw paper L2 capture

/opt/liquidity-migration/data/bybit-long-demo-event          # LONG signals, market cache and cycle telemetry
/opt/liquidity-migration/data/bybit-long-paper-event         # LONG paper signals, market cache and cycle telemetry
/opt/liquidity-migration/data/bybit-continuous-demo-event    # CONTINUOUS signals, market cache and cycle telemetry
/opt/liquidity-migration/data/bybit-continuous-paper-event   # CONTINUOUS paper signals, market cache and cycle telemetry
```

The account roots, not mutable sleeve trade rows, are execution and accounting
authority. Demo and paper sleeves publish absolute component targets to their
respective inboxes. The demo account owner alone mutates Bybit; the paper owner
alone advances the deterministic execution twin. Raw captures are intentionally
independent so a healthy demo feed cannot hide a dead paper feed.

Which sleeves are requested at any moment is `deploy/sleeves.env`; what is
actually running is systemd state plus the resolved host environment and the
read-only verifier. Turning a sleeve off stops new strategy decisions but does
not delete canonical account history or imply that its existing target is flat.

VPS ledger history restarted from a clean slate at the 2026-06-09 full rebuild
(all prior demo/paper history lost — see STATE.md). The research roots and VPS
roots remain fully independent.
Do not point any live account or sleeve root at a research root. Sleeve roots
retain forward signal inputs, caches and cycle telemetry; compatibility
Parquet views are not position or P&L authority. The account journal and its
rebuildable projections own that state.

Each sleeve has a target-publishing paper shadow on its own decision root
(long: `data/bybit-long-paper-event`, continuous:
`data/bybit-continuous-paper-event`) with the same profile, universe and cadence.
The shared paper owner fills accepted aggregate targets against captured L2,
subject to the same verified instrument rules and account kernel as demo.
Comparing the paper and demo account journals measures model-versus-demo
execution differences without pretending that the old sleeve-local idealized
fill ledgers are authoritative.

Use `scripts/ops.sh account-parity` for structural comparison of historical,
paper and demo account journals. It records hashes and refuses empty journals,
but it does not by itself prove shared market-tape or strategy-scheduler parity;
see `docs/account_execution_cutover.md`.

The former `scripts/reconcile.sh` and sleeve-local `reconcile-*` commands were
retired on 2026-07-13. They read compatibility projections rather than the
account journal and could therefore produce agreement without validating the
current owner. Historical reports from those tools remain historical evidence;
they are not a current operational gate. Use the exact research command for a
new model/PIT claim, and use the account cutover acceptance checklist for a
runtime claim.

Do not use ad hoc current-universe or temporary recent roots for a historical
universe claim. They remain useful for explicitly scoped diagnostics. A live
`exchangeInfo` snapshot is not a historical membership source; see
`docs/pit_gate.md` and `docs/governance.md`.
