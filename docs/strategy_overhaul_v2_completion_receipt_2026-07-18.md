# Strategy Overhaul V2 Completion Receipt and Evidence Card

## Decision

The first V2 diagnostic cycle is closed with **no qualifying thesis**. The
reserved holdout remains untouched, so no Phase-5 outcome was generated and no
Phase-6 strategy/runtime change is applicable. This is a useful negative
result: the diagnostic populations contain path structure, but no actionable
LONG contrast clears the frozen post-cost gate, and CONTINUOUS cannot form the
exact current-profile comparator while residual-momentum provenance is invalid.

This receipt is exploratory research evidence, not deployment, sizing, paper,
demo, mainnet, or real-money authority.

## Registered scope and identities

- Venue/root: Bybit,
  `C:\Users\user\SHARED_DATA\bybit_full_pit`.
- Discovery signals: `[2021-05-01, 2024-12-01)`, 43 complete calendar months.
- Embargo: December 2024; late-November lifecycle exits may extend into it, but
  no December signal enters inference.
- Reserved holdout: `[2025-01-01, 2026-07-06)`; no `bybit/holdout` directory was
  created or read. The already generated `[2026-07-05, 2026-07-06)` structural
  partition was also not used for outcome inference.
- Completion contract SHA-256:
  `702ab2e84e0c6acdc5c14acd251a60a63f8fdca68928b0109b2d440999876cc8`.
- Recovery contract SHA-256:
  `d572818f7098a4ffda52c325881a98e49ed952b01b626c4e478c5288cb580095`.
- Bounded-account recovery SHA-256:
  `b9e3892d96daaa60617e0ea9b5dbde68a78bae9b55c619eae5d1cd52d3f282e6`.
- Successful evaluator commit:
  `ebe99c3722eebf823ee119a507be175898b8203f`.
- Active comparison stayed disabled because all 23 pinned baseline payloads
  were absent. None was recreated.

The final run completed in 490.707 seconds. The two preserved failed account
replays used approximately 74 and 19 minutes; together with the successful
run, claim-bearing Phase-3 analysis remained below the cumulative two-hour
stop. They are retained as ignored failure receipts at
`.phase3-analysis.failed-fsync-2026-07-18` and
`.phase3-analysis.failed-atomic-2026-07-18`.

Final production-line accounting is 3,772 inherited lines, 37 completion-cycle
candidate-runner lines, and 1,691 analyzer lines: **5,500 cumulative lines**, at
but not above the prospectively amended ceiling. The cycle added one read-only
analysis entry point and no generalized runner/cache framework.

## Final four-payload artifact

Root:
`reports/strategy-overhaul-v2/diagnostic-epoch-2026-07-17/phase3-analysis/`

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| `manifest.json` | 7,308 | `48c34b7612eb7a0d3e8603908df0633b8640705f4cd571c483577fbab2465269` |
| `diagnostics.json` | 37,989 | `5fbcf06904454ca39ad3138bfc0cc80acb4f59658ccd58293f38783e87910274` |
| `barebones_ledger.parquet` | 1,935,119 | `368a7c04640dd362179d4c00897948d036ce38dc6136da12eedd47b4b6c64ddd` |
| `barebones_curve.parquet` | 111,503 | `9e7fe184e47e2fd9090e91f8a62898e174659b56854d89ba86cf0874b587a725` |

The manifest's canonical payload hash is
`0a14862522af6e37ea05facbb47f9f4564e6f298ccb4a2d3559f5a79b0f06d9d`.

The manifest identity, all three declared file hashes, row-level
`net = gross + cost + funding`, ledger/curve sleeve sums, fixed-notional equity
recurrence, exit-before-entry occupancy, capacity, sample-key hashes, and
sampled journal final state/event hashes were independently recomputed after
publication. All matched. The verified success scratch root contained 1,381
files and 11,253,170 bytes and was sent to the Windows Recycle Bin after those
checks so the retained successful artifact remains exactly four payloads; it is
recoverable from the Recycle Bin but is not part of the research artifact.

The raw manifest field `outcomes_first_opened_at_utc` is run-local to the final
recovery. The experiment was actually spent when the first failed replay
created its working root at approximately 2026-07-17 22:31 UTC. The recovery
contracts, not that field name, preserve the true exposure history.

## Code and local quality gate

- Repository doctor: ready; Python 3.13.6, all 26 exact dependency pins, eight
  mirrored skill files, and Graphify availability verified.
- Whole-repository Ruff: passed.
- Linux-targeted mypy: passed for all 99 package/supported-script sources and,
  separately, the V2 analyzer.
- Focused funnel/analyzer tests: 13 passed with warnings treated as errors.
- `scripts/dev.sh check` cannot complete on this Windows host: after doctor and
  Ruff pass, mypy reports 143 platform-stub errors for intentional POSIX APIs
  such as `fcntl`, `geteuid`, `O_NOFOLLOW`, and `fchmod`, so its pytest stage is
  not reached. This is an explicitly scoped host limitation; a full Linux gate
  was not run locally.
- Graphify was refreshed after the new analysis entry point: 3,114 nodes,
  11,512 edges, and 26 communities.

## Integrity and population

- 640,367 unique source decisions across 1,300 UTC signal dates: 8,214 LONG
  and 632,153 CONTINUOUS.
- 225,696 exactly matched admitted label rows: 5,850 LONG and 219,846
  CONTINUOUS. There were no duplicate source keys or embargo leaks.
- All 640,367 membership rows use direct
  `bybit_public_trading_archive` evidence; `membership_inferred=true` occurs
  zero times. The one shared limitation is that local URL/source labels are not
  cryptographic publisher authentication.
- Independent raw-feature replay reproduced every LONG/CONTINUOUS source key.
  Final analysis reverified 276,132 unique candidate inputs across all 43
  months. Funding identity covers 279,856 files / 666,988,449 bytes; portfolio
  lifecycle read 274,495 kline files already covered by candidate manifests.
- The CONTINUOUS root cannot prove the exact active residual-momentum comparator
  because its source lacks the required `is_provisional` provenance.

## Funnel and path diagnostics

| Sleeve | Source rows | Barebones accepted | Source waves / dates | Main attrition |
| --- | ---: | ---: | ---: | --- |
| LONG | 8,214 | 5,850 | 2,409 / 990 | history 1,627; liquidity 737 |
| CONTINUOUS | 632,153 | 219,846 | 31,095 / 1,296 | history missing 90,827; liquidity 321,480 |

Equal-date estimates first average simultaneous candidates within a wave, then
waves within a UTC date. Intervals are the registered 10,000-replicate UTC-date
block bootstrap.

| Sleeve/path | Equal-date mean | 95% block interval | Candidate median | Missing |
| --- | ---: | ---: | ---: | ---: |
| LONG 24h | +0.2208% | [-0.3170%, +0.7686%] | -0.5973% | 2 |
| LONG 72h | +0.5996% | [-0.3909%, +1.6860%] | -0.6086% | 9 |
| CONTINUOUS 24h | +0.3221% | [+0.0385%, +0.6150%] | +1.1721% | 67 |
| CONTINUOUS 72h | +0.3395% | [-0.2256%, +0.9057%] | +2.2277% | 373 |

LONG 72h equal-date MAE/MFE are -10.49% / +15.02%; CONTINUOUS are
-13.37% / +11.38%. These describe broad path dispersion, not executable alpha.

## Barebones fixed-capital portfolio

The full portfolios use USD 10,000 per trade against fixed USD 1,000,000
capital. They are model-based lifecycle/cost/funding diagnostics. Because full
production account replay was superlinear and crossed the stop-loss, exact
kernel/event/hash verification is prospectively bounded to the 100 smallest
SHA-256 `source_key` values per sleeve. It passed for both samples, but does not
make the full ledger account-reconciled.

| Metric | LONG | CONTINUOUS |
| --- | ---: | ---: |
| Trades / symbols | 1,899 / 350 | 16,745 / 424 |
| Gross return | +3.2782% | +36.5968% |
| Cost contribution | -8.5455% | -40.8873% |
| Funding contribution | +2.0369% | -15.9352% |
| Net return | **-3.2304%** | **-20.2257%** |
| Additive max drawdown | -8.6009% | -38.7379% |
| Capital turnover | 37.98x | 334.90x |
| Mean / maximum open | 3.35 / 10 | 11.78 / 25 |
| Worst day | -1.7938% (2024-01-03) | -5.2129% (2022-11-10) |

LONG exits are 1,100 max-hold, 593 stop, 202 take-profit, and four data-end.
CONTINUOUS exits are 14,086 max-hold, 2,649 take-profit, and ten data-end.
Funding is modeled for 1,895 / 16,730 trades and partial for 4 / 15.

Post-hoc concentration descriptions (not thesis selectors): the ten worst
symbols contribute 14.03% of all negative-symbol LONG contribution and 24.03%
for CONTINUOUS; the ten worst days contribute 12.15% and 10.39% of respective
negative-day contribution. Both sleeves lose on 156 common dates; their summed
negative contribution on those dates is -67.47% of fixed capital, with the
worst simultaneous-loss date -3.3277% on 2024-08-06. There are only 32
same-symbol/same-entry-timestamp cross-sleeve overlaps.

## Phase-4 candidate decision

The applicable LONG round-trip hurdle is 45 bps. Independent recomputation of
every registered Q4-Q1/categorical 24h effect exactly matched the artifact:

| LONG family | 24h effect | 95% block interval | `abs(effect) / cost` | Decision |
| --- | ---: | ---: | ---: | --- |
| signal strength | -15.81 bps | [-121.67, +95.73] | 0.35 | below cost |
| close location | -35.50 bps | [-148.31, +79.25] | 0.79 | below cost |
| volatility / ATR | +26.52 bps | [-82.75, +137.37] | 0.59 | below cost |
| turnover / liquidity | -17.85 bps | [-116.86, +78.58] | 0.40 | below cost |
| listing age | +38.68 bps | [-92.16, +171.10] | 0.86 | below cost |
| active BTC+ETH regime | +38.00 bps | [-77.55, +146.77] | 0.84 | below cost and no profile change |

`diagnostics.json` incorrectly marks some LONG rows eligible and names close
location because the implementation omitted `economic_score >= 1` from its
boolean gate even though it calculated `economic_score = 0`. That raw selector
field is invalid. The frozen prose rule is unambiguous: a thesis needs plausible
post-cost benefit. Applying it yields no LONG thesis. The implementation is
corrected prospectively with a regression test in the completion commit; the
spent artifact is not rewritten or rerun.

CONTINUOUS has useful exploratory leads but no eligible thesis. In particular,
high-minus-low `source_composite` is +65.74 bps at 24h with a [+24.15,
+108.01] bps interval versus 23.59 bps median modeled cost. It weakens from
+144.02 bps early to +6.15 bps late, and the exact active comparator is
unconstructable because residual-momentum provenance is invalid. The 72h
high-volatility contrast is also positive (+140.57 bps, interval [+31.13,
+250.11]) while its 24h eras reverse sign. These are leads for a newly
registered causal-repair cycle, not evidence to change the current profile.

## Plan closure and next useful work

- Phases 0--2: previously closed.
- Phase 3: complete with the absent baseline and bounded-account limitations
  above.
- Phase 4: complete; every considered candidate is recorded and none qualifies.
- Phase 5: not activated; no thesis contract and no holdout outcome.
- Phase 6A/6B: not applicable; no implementation, offline parity run, or
  demo/paper epoch is authorized.

The diagnostics are useful for forming new theses, especially a prospective
repair of CONTINUOUS residual-momentum provenance followed by an exact
current-profile comparator, and cost/funding mechanisms that can explain why
positive source paths become negative portfolios. Any such work needs a new
contract and genuinely untouched data. The current holdout must not be mined
for an unregistered replacement rule, and the observed 72h listing-age or
volatility patterns must not be relabelled as confirmed alpha.
