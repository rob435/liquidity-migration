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
