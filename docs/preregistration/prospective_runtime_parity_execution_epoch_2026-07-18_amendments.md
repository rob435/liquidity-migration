# Prospective runtime-parity epoch: append-only amendments

This file extends, but does not rewrite, the immutable base contract
`prospective_runtime_parity_execution_epoch_2026-07-18.md`. The base contract
bytes remain pinned by SHA-256
`15edc498adf2bd068c33ff2f791fa3e46f161196db673a839adcf317aba35a31`
in the already-published raw snapshot receipt. Feature and later receipts must
pin this amendment file separately.

## Amendment 4: canonical leading-listing padding

Registered 2026-07-18 14:54 UTC after the first feature-builder attempt failed
closed in its first input chunk and before any feature, target, return, trade,
or P&L output was generated or inspected. The failed working directory is
retained as `.bybit-baseline.working` under the registered feature parent.

The structural check found six leading hourly rows for `MAGICUSDT` on
2022-12-13 where all four OHLC fields are null and both base volume and quote
turnover are exactly zero. The first subsequent row has complete OHLC and
nonzero activity. This is the archive's canonical pre-first-trade densification
pattern, not a priced bar. Existing production owners already give it the
intended semantics: the PIT coverage gate counts densified rows as data
presence, LONG daily aggregation retains the day while executable first-bar
fields remain unavailable, and CONTINUOUS removes rows whose close is null.

Input validation is therefore narrowed prospectively as follows:

- allow and separately count a row only when all OHLC fields are null and both
  base volume and quote turnover are exactly zero;
- continue to reject partial-null OHLC, non-finite or non-positive priced bars,
  nonzero all-null rows, negative/non-finite volume or turnover, blank keys,
  duplicate `(symbol, ts_ms)` keys, and date/timestamp disagreement; and
- make the first and second verified-read padding counts match exactly.

This changes no feature formula, membership rule, rank population, tolerance,
model, outcome boundary, or decision rule. It makes an existing raw-data
representation explicit before the affected feature surface is produced.

## Amendment 5: deterministic runtime-comparator ports and active authority

Registered 2026-07-18 15:47 UTC after source/config inspection and the
completed outcome-blind full-ledger reconciliation, but before any historical
active source-decision, target, lifecycle, return, or P&L transcript was
generated or inspected.

The deployed target producers, not the legacy historical equity engines, own
the active strategy semantics for this parity claim. In particular, source
inspection found that the standard CONTINUOUS component engine applies a
historical `entry_crowding_max_fresh=2` rule and independent per-component
capacity, while the deployed target producer exposes no crowding rule and owns
one shared 25-component book with at most five new component targets per cycle.
The standard curve also omits the deployed accepted-decision BTC-risk state.
Those are retained diagnostic differences, not rules to copy into the runtime
comparator and not authorization to change either strategy.

The offline comparator is therefore frozen to these production authorities:

- LONG uses `long_v11a_profile`, the deployed demo sizing controls
  (`notional_multiplier=1`, `entry_leverage=10`, five new entries per cycle),
  and the production candidate, target, account, and protection owners;
- CONTINUOUS uses `apply_continuous_demo_profile` with the deployed demo
  controls (`btc_trend_gate=uptrend`, `max_active=25`, five new component
  targets per cycle, `entry_leverage=10`, `notional_multiplier=10`, and
  `per_position_notional_pct_equity=2`), including its three ordered component
  definitions, shared capacity, inverse-volatility sizing, and complete
  accepted-decision BTC-risk evidence chain; and
- deterministic equity is USD 1,000,000. Route, clock, market, journal, and
  execution adapters remain explicitly historical and carry no venue-rule,
  latency, capacity, or fill-quality claim.

The source-decision window is `[2023-03-01, 2026-07-10)` for LONG and the
code-owned CONTINUOUS inception window `[2023-04-01, 2026-07-10)` for
CONTINUOUS. Run-scoped features and manifest membership remain the only
historical ranking inputs. At each eligible whole-hour decision boundary, the
frozen hourly close is the deterministic market reference. Software
stop/take-profit evaluation may observe those hourly closes only; it must not
infer an intrabar crossing from hourly high/low or incomplete five-minute
data. Forward target captures retain ownership of real intrabar/runtime parity.

Parity artifacts must separately retain (a) the full frozen source population
and every gate decision, (b) the dynamic accepted target/lifecycle transcript,
(c) request content hashes and BTC-risk predecessor/evidence hashes, and (d)
account journal/state identities. They may compare the runtime transcript with
the legacy historical engine only as a labelled diagnostic; a mismatch cannot
be silently repaired, pooled away, or interpreted as alpha. No return, thesis,
profile, deployment, mainnet, or real-money conclusion is authorized.
