# Strategy Overhaul V2 comparator and accounting repair, 2026-07-18

## Prospective contract

- **ID / owner / registered time / study mode:**
  `strategy-overhaul-v2-comparator-accounting-repair-2026-07-18`;
  repository owner with Codex operator; 2026-07-18 10:04 UTC. Engineering
  parity and provenance checks are outcome-blind validation. The matched
  comparator and gate attribution are exploratory because their historical
  market outcomes and the motivating characteristic effect have already been
  inspected. No confirmatory outcome is active under this contract.
- **Reason to reopen:** the completed V2 cycle identified two correctable
  limitations rather than a new unregistered search: the canonical research
  residual-momentum table lacks the `is_provisional` field required to build
  the active CONTINUOUS comparator, and exhaustive production-account replay
  was reduced prospectively to a 100-key sample after the persistence path
  became superlinear. Repairing those defects is a permitted new evidence
  cycle under `docs/governance.md`.
- **Prior exposure:** all V2 discovery outcomes in
  `[2021-05-01, 2024-12-01)` are spent. In particular, CONTINUOUS
  `source_composite` Q4-Q1 at 24h is known to be +65.743586 bps with discovery
  Q75 `0.9800995024875622`, and its late-era effect is known to be much weaker.
  Corrected aggregate active-profile results through 2026-07-16 are also
  exposed. The V2 reserved holdout `[2025-01-01, 2026-07-06)` remains
  outcome-uninspected for the proposed treatment, but the underlying market
  period is not globally pristine because current-profile aggregates used part
  of it.
- **Intended action:** establish whether an exactly reconstructed active
  CONTINUOUS control can be compared with the V2 source population under one
  accounting/economic model; identify which current gates create or destroy
  net economics; and decide whether the one already nominated
  `source_composite` lever is sufficiently identified to justify a later
  one-shot holdout contract. No profile, runtime, deployment, order, size, or
  capital change is authorized here.

## Identities and immutable inputs

- Generating code at registration:
  `674e3ebd688f6237b87cb4516ea03e9dd7828b97`.
- Bybit data root: `C:\Users\user\SHARED_DATA\bybit_full_pit`.
- V2 completion contract SHA-256:
  `702ab2e84e0c6acdc5c14acd251a60a63f8fdca68928b0109b2d440999876cc8`.
- V2 Phase-3 manifest SHA-256:
  `48c34b7612eb7a0d3e8603908df0633b8640705f4cd571c483577fbab2465269`.
- V2 diagnostics SHA-256:
  `5fbcf06904454ca39ad3138bfc0cc80acb4f59658ccd58293f38783e87910274`.
- Legacy residual-momentum input at registration: 472,879 rows, schema
  `(symbol: String, ts_ms: Int64, residual_momentum: Float64)`, SHA-256
  `547259477d4d33d70e904a0226366338695d503794e7856aa72fb9c6079d9f6f`.
  It is an invalid active-comparator input because it lacks provenance. It is
  read-only evidence and will not be overwritten by this experiment.

Every outcome-bearing run manifest must pin the clean code commit, this
contract, active profile/config identities, all raw kline/PIT/funding inputs,
the reconstructed RMOM payload, the inherited V2 payloads, commands, tested
set, and output hashes. A later code commit is permitted only for the exact
repairs and runner declared here; the manifest records it.

## Surface and exposure boundary

The matched exploratory comparator uses Bybit USDT linear perpetual decisions
in `[2023-07-17, 2024-12-01)`. This start is the corrected active baseline's
fixed historical start and lies inside the already spent V2 discovery surface.
December 2024 remains an embargo for signals; causal exit and feature-maturity
reads may extend only through `[2024-12-01, 2024-12-05)`.

The RMOM repair uses the one current causal formula, fixed history start
`2023-03-01`, and exclusive build end `2024-12-05`. It writes only below:

```text
reports/strategy-overhaul-v2/comparator-accounting-repair-2026-07-18/
```

It must not replace the shared-root feature file. The full-account repair may
read the already published V2 barebones ledger across its complete discovery
window, but it computes no new price outcome. No process under this contract
may create, generate, open, hash, schema-scan, or infer any path/ledger payload
below `bybit/holdout` or any thesis holdout root.

## RMOM provenance repair gate

There is exactly one RMOM build: the current
`scripts/precompute_residual_momentum.py` formula, factor set, seven-day
rolling window, three-day causal shift, four-observation minimum, and explicit
provisional owner. No alternate start, shift, window, factor set, missing-value
rule, or favorable tail treatment may be inspected.

Before the rebuilt feature can enter a comparator:

1. schema, daily keys, uniqueness, finite values, and non-null boolean
   `is_provisional` must pass the current stable-RMOM validator;
2. every rebuilt stable key in the comparator window must have a matching
   legacy `(symbol, ts_ms)` value; missing or extra rebuilt stable keys stop the
   comparison;
3. matching values require identical non-finite positions and
   `rtol=1e-10`, `atol=1e-12`; and
4. all rows consumed by a decision must be stable and causally available.

The legacy table's agreement can validate values on this spent window; it
cannot retroactively prove its missing provenance field. The rebuilt payload
owns that provenance.

## Account and replay repair gate

The performance repair may change only replay/cache complexity and run-scoped
persistence representation. Production event construction, decision order,
target content, reducer transitions, prices, costs, funding, risk decisions,
fills, positions, cash/equity arithmetic, lifecycle, and event/state hashes
must remain unchanged.

- Remove repeated whole-history event/list/map copies from the in-process
  account-journal cache while retaining the atomic segment as the commit point
  and preserving prior-state visibility to concurrent readers.
- The single-process Windows research adapter may buffer original kernel
  transaction batches and materialize consecutive whole batches into compact
  authoritative segments. It may never split or reorder a kernel batch. It
  must hash the original batch-boundary sequence so compaction cannot hide a
  semantic boundary change. This carries no POSIX durability or concurrency
  claim.
- Exact regression requires identical discrete decisions, event rows and
  order, event IDs/hashes, final state hash, fills, closes, P&L, and ledger keys
  on deterministic fixtures and on the previously frozen 100-key samples.
  Continuous numeric values require `rtol=1e-12`, `atol=1e-12` with matching
  null/NaN positions.
- The repaired path then replays every published V2 barebones trade: 1,899
  LONG and 16,745 CONTINUOUS. Both sleeves must end flat; every expected entry
  and exit fill must exist; journal verification, ledger P&L identities, and
  full-trade/source-key coverage must pass. Any mismatch invalidates the
  affected performance/accounting claim and stops outcome comparison.

Preserve failed and partial roots. Checkpoint only at deterministic sleeve or
segment boundaries. One full replay attempt may run for at most two measured
hours; an outcome-blind performance repair may be registered before retry, but
the full claim may not be reduced to another sample.

## Exact matched control

The comparator's effective source unit is
`(venue, symbol, signal_ts_ms, component)`, simultaneous wave is UTC decision
timestamp, and uncertainty block is UTC signal date. Historical PIT membership
must be applied before every cross-sectional rank. Stablecoin exclusions,
feature timing, one-hour confirmation, and the current active configuration
come from code rather than old report prose.

The active control must reproduce, at fixed USD 1,000,000 capital and a USD
10,000 base decision scale:

- stable RMOM bottom quartile before active composite/decile ranking;
- active decile 9, liquidity floor, prior-day BTC trend, 240-day age, and the
  three code-owned event components and weights;
- inverse-volatility and accepted-decision BTC-risk sizing;
- component capacity/new-entry ordering, same-symbol/target handling, TP12,
  24-hour maximum hold, conservative same-bar ordering, exact settlement
  funding, modeled fees/spread/impact, and the production account kernel;
- point-in-time membership and the same raw kline/funding inputs as the
  barebones side.

Before historical comparator outcomes are opened, deterministic fixture tests
must prove source/decision/target/account parity between this comparator and
the active target-producing functions for every listed lever. Any active
behavior that cannot be reconstructed is a failed exact-comparator gate, not a
silent omission. The full runtime hedge is reported separately because it is
portfolio overlay state rather than a source-entry lever; it cannot repair a
failed component comparator.

## Frozen exploratory tested set

The complete source-component tested set is the exact active control plus the
following one-at-a-time diagnostic ablations, each otherwise identical:

1. RMOM gate disabled while retaining only provenance-valid RMOM rows;
2. prior-day BTC trend gate disabled;
3. 240-day age gate disabled;
4. active event gate disabled;
5. crowding gate disabled;
6. inverse-volatility sizing replaced by flat sizing; and
7. accepted-decision BTC-risk sizing replaced by multiplier 1.0.

These seven cells attribute existing behavior on spent data. They cannot
nominate a new lever, and no combination, threshold, alternate order, or best
subset may be run.

There is exactly one thesis-eligibility treatment:

```text
active control AND causal source_composite >= 0.9800995024875622
```

`source_composite` must be computed on the PIT-valid pre-RMOM source
cross-section using the existing `max_ret168` feature and V2 rank convention,
then carried unchanged into the active control. There is no Q25, Q50, rounded,
late-era, symbol, time, or multi-feature treatment. The known decay is an
explicit fragility prior, not a reason to change the constant.

## Exploratory decision rule

Report control and every fixed cell's unique sources, waves, dates, symbols,
trades, gross/cost/funding/net return, turnover, occupancy, maximum drawdown,
worst day, concentration, account identities, and paired daily differences.
Use a 10,000-replicate UTC-date block bootstrap with seed `20260718`.

The source-composite treatment qualifies for a later holdout contract only if
all structural/accounting gates pass and all of the following are true on the
matched discovery window:

1. its fixed-capital net return is greater than the active control;
2. the 95% paired date-block interval for treatment-minus-control net return
   has lower bound greater than zero;
3. its point delta is positive in both fixed eras
   `[2023-07-17, 2024-04-01)` and `[2024-04-01, 2024-12-01)`;
4. turnover is no greater than control; and
5. maximum drawdown and worst-day return are each no worse than control by more
   than 0.50% of fixed capital.

Failure of any structural gate yields `invalid`. Passing structure but missing
support or uncertainty yields `inconclusive`; a non-positive full-window point
delta yields `contradicts`. Because discovery is spent, even `qualifies` is
thesis-selection evidence only, not alpha support.

## Conditional next phases

If and only if the treatment qualifies, create a separate thesis-specific
confirmatory preregistration before any V2 holdout payload is generated or
read. It must contain exactly the active control and the one fixed treatment,
one read of `[2025-01-01, 2026-07-06)`, paired UTC-date inference, net/drawdown/
turnover guardrails, and `supports`/`contradicts`/`inconclusive` rules. No
holdout retune is permitted.

If and only if that holdout supports the treatment, implement the smallest
single-lever profile change and require exact source decisions, targets,
lifecycle events, account hashes/state, and declared numeric tolerances in an
offline/shadow parity run. A forward demo/paper execution epoch is a separate
operational contract. This instruction authorizes neither new external demo
orders nor changes to the currently authorized fleet; existing verified
journal observations may be used only under a separately frozen execution-
measurement rule. Mainnet and real money remain unauthorized.

## Artifacts, resources, and non-conclusions

The successful repair root retains a manifest, repaired RMOM payload, matched
source/decision ledger, portfolio/account ledger, daily curve, attribution
JSON, and compact verified account-journal archive. Standard active curve
outputs may be retained in a named subdirectory but are compatibility
diagnostics, not substitutes for the matched comparator. Every payload is
hash-pinned; failed roots remain visible.

Use concurrency one for outcome-bearing builds. Cap Polars threads at six.
Preflight must verify that the holdout path is absent and that no requested
input reaches it. No result establishes independent market replication,
calibrated venue slippage, complete live-runtime hedge parity, deployment
readiness, size beyond USD 10,000 base decisions, mainnet readiness, or
real-money authority.
