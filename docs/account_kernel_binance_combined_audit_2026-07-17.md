# Account-Kernel Binance Combined Branch Audit, 2026-07-17

Status: compact finding preserved before deletion of the obsolete remote
research branch. This audit is diagnostic; it is not performance confirmation
or deployment authority.

## Answer first

The apparent funding inconsistency between the Big-PC branch's pre- and
post-kernel CONTINUOUS outputs was not a different funding calculation. The
pre-run was TP10 and the post-run was TP12. TP12 changed exits and holding
intervals, which changed the set of settlements charged. For every trade whose
entry/exit interval and weight remained identical, the old funding return and
event count matched exactly.

The branch did find a real, separate account-kernel replay defect: active marks
were dropped before later chronological exits were submitted, and an old
active mark could compete with the current decision's execution price. Current
`main` already contains the equivalent independently developed correction, so
the branch should not be merged.

For funding, neither old checkout was right. Both shared the same modal-cadence
algorithm, which undercharged temporary hourly and four-hour settlement
regimes. Current `main` is the right accounting implementation because it
charges exact settlement timestamps and fails closed on conflicting semantics.

## Source identities

- Obsolete branch head:
  `378fef44bdc9f400a5c09eded1d9c506bbd8502b`
- Merge base with current `main`:
  `c7ff0806e935504a0263380eb54c0c0e4c8b01dc`
- Branch defect-fix candidate:
  `5b9a05325e4eb294dafed21720357d08279706a7`
- Branch report:
  `reports/autonomous-improvement/2026-07-16-account-kernel-backtest-rerun.md`
- Compared Binance raw cells:
  `compat-v2-rm-pre-continuous-binance` and
  `postfix-completion-continuous-binance`

Relative to the merge base, the branch contains 701 files and 1,539,430
insertions. Its own manifest describes 90,897 generated files and about 1.4 GB
of evidence, plus Windows-only adapters and failed native-platform gates. That
bulk is not an appropriate merge into `main`.

## Exact TP10-versus-TP12 comparison

The trade-ID set is identical within each component. Actual take-profit prices
are 10% below entry in every pre row and 12% below entry in every post row.

| Component | Rows / common IDs | Exit timestamps changed | Exit reasons changed | Funding rows changed | Old -> new events | Same interval + weight | Funding mismatches in that subset |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `turn3p3` | 1,020 | 204 | 105 | 129 | 3,422 -> 3,646 | 816 | 0 |
| `turn4p3` | 931 | 188 | 96 | 119 | 3,127 -> 3,346 | 743 | 0 |
| `turn4p5` | 790 | 176 | 84 | 111 | 2,587 -> 2,805 | 614 | 0 |

The TP change reduced take-profit exits from 329 to 224 in `turn3p3`, 300 to
204 in `turn4p3`, and 278 to 194 in `turn4p5`. It changed exit price and net
return in 329, 300, and 278 rows, respectively. Those lifecycle changes fully
explain why aggregate funding differed. Subtracting the post TP12 curve from
the pre TP10 curve and calling the result a kernel or funding effect is invalid.

## Which fixes belong on main

The account-mark diagnosis was correct. Branch commit `5b9a053`:

1. retained locally closed LONG positions until every chronological exit group
   reached the account kernel; and
2. excluded symbols targeted by the current decision batch from the auxiliary
   active-mark map, letting the current execution reference be authoritative.

Main commit `be6367fd` independently implemented those same behaviors inline
in `long_native.py` and `continuous_events.py`. The clean replacement benchmark
at `b095d5c` completed all four cells through the common account kernel, and the
full repository gate had already passed after the lifecycle corrections. There
is no missing account-mark change to cherry-pick.

Before the exact-settlement correction, branch commit `5b9a053` and prior-main
commit `0c90a58` had the identical `trade_lifecycle.py` blob
`acbe9e67f117ebe91a1fcccff4a50c8a100b0406`. Their same-interval equality is
therefore expected; it does not validate their common funding model. Main
commit `d91b6db` replaced modal cadence with distinct settlement timestamps and
was checked against all 27 funding rows in the owner-supplied Bybit paste, with
maximum absolute rate difference `2.89139633283665e-10` after timezone and sign
normalization.

Current `main` also contains two correctness fixes absent from the obsolete
branch: stable aged-out RMOM keys (`b163fd5`) and chronological terminal-tape
closure (`b095d5c`). The corrected current comparison baseline is
`docs/strategy_overhaul_v2_baseline_2026-07-17.md`.

## Retained limitations

The Big-PC report correctly says its net accounting did not reconcile to the
account journal, Binance funding was partial, CONTINUOUS lacked a manifest
receipt, and native Windows quality gates failed. Its patched TP12 curves are
useful historical diagnostics only. Deleting the branch removes an obsolete
carrier for enormous generated artifacts; it does not erase this conclusion or
promote the replacement benchmark.
