# Autonomous improvement cycle 006: fetch sparse archive gaps directly

## Finding

- Audit timestamp: `2026-07-16T01:47:40Z`.
- Audited commit: `cd2abdcbf87869af924d4ae931c15852e0d4b80d`,
  plus the named local changes.
- `_download_api_hourly_group()` selected the first and last missing manifest
  dates for a symbol, then queried every hour between them. It filtered API
  results back to the requested dates before writing, so outputs were scoped,
  but request cost and rate-limit exposure scaled with the calendar span rather
  than with missing coverage.
- This is common after partial recovery or isolated corrupt partitions: a pair
  of holes separated by otherwise complete history caused completed dates to be
  downloaded again and discarded.

## Prospective measurement

The production-default regression selects only `2020-01-01` and `2025-12-31`
for one symbol, uses the existing 1,000-hour request limit, and records the API
windows. The pre-change planner required 53 calls covering 52,608 hours. The
new planner makes exactly 2 calls covering the 48 required hours:

```text
selected missing dates: 2
API calls before: 53
API calls after: 2
reduction: 51 calls (96.23%)
requested hours before: 52,608
requested hours after: 48
request-hour reduction: 52,560 (99.91%)
result dates: [2020-01-01, 2025-12-31]
result statuses: [downloaded, downloaded]
```

This is a deterministic request-count measurement against the planner, not a
claim about wall-clock network speed or Bybit quota policy.

An initial contiguous-run implementation achieved the sparse result but was
not retained: an independent edge review found that `2025-01-01` plus
`2025-01-03` fit in one default-size request before but would take two. A second
prospective regression failed on that implementation. The final bounded greedy
planner keeps the nearby case at one request while retaining the multi-year
2-call result.

## Implementation

- Missing dates are normalized into required UTC day intervals.
- Each request starts at the earliest uncovered required hour and packs every
  later required interval that fits inside the existing limit. Its end is
  trimmed to the last required hour in that window.
- Fetch parsing, timestamp-to-date filtering, deduplication, densification,
  partition writes, result ordering, and accounting remain unchanged.
- Nearby missing dates can still share a request, so planned call count cannot
  exceed the prior continuous tiling. Long gaps are skipped rather than fetched
  and discarded.

## Validation

- Archive-manifest suite: 66 passed in 0.37 seconds.
- The checked-in property regression covers 2,550 nonempty date/limit
  combinations. Every required hour is covered, each window respects the
  limit, windows do not overlap, and call count never exceeds the former
  continuous-span planner.
- Full local pytest suite after the greedy edge correction: 1,609 passed in
  20.98 seconds.
- Repository-wide Ruff: passed.
- Package-wide mypy: 85 modules passed.
- Focused mypy for `archive_manifest.py`: passed.
- Locked Python 3.11 archive suites: 102 passed in 0.69 seconds.
- Locked package-wide mypy: 85 modules passed.
- Locked Ruff across source, scripts, and tests: passed.
- `git diff --check`: passed locally and in the independent locked check.

The tests use a fake API and temporary partitions; no external request or data
mutation occurred. An independent read-only review found no semantic blocker in
the final planner.

## Residual risk and next candidates

- Provider responses can still be partial or empty; the existing result logic
  records those dates accordingly. This change removes irrelevant windows but
  does not introduce completeness evidence the API does not provide.
- Each symbol group still runs independently, which is required because API
  queries are symbol-specific.
- Highest-value remaining operational candidates include serializing deploys
  across branches on the shared VPS and making stale-lock recovery
  process-aware. Either requires a separate prospective failure case before an
  implementation claim.
