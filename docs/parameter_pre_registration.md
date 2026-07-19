# Committing Work Into The Rolling Record

`docs/governance.md` (the Progressive Evidence Model) is the governing
policy. This page is the mechanical how-to.

## Lane 1 — exploration needs nothing

Explore freely on any already-seen data. Label outputs exploratory and note
which data they touched. That single provenance note is the only ask,
because it is what keeps Lane 2 honest later.

## Lane 2 — the commit is the registration

To move a prototype into the rolling forward record:

1. Put its exact config (rule, parameters, feature definitions, cost model)
   in the repository and commit. The commit hash and date ARE the
   registration — no separate contract document is needed.
2. Declare the scoring recipe in the config or its manifest: metric,
   comparator/baseline, and the grid if there is one (all cells report).
3. Let the scorer append one row per config per new day. The config's
   evidence is the run of days after its commit; editing the config starts
   a new run under the new commit.

## Promotion note

When a rolling record earns a live change, record five lines alongside the
deploy change point:

```text
Claim:
Config commit:
Forward record (days, net delta vs baseline, tail behavior):
Decision:
Date:
```

## Optional historical reserves

If an untouched historical window exists for a genuinely new idea, it can be
opened once for instant forward-style evidence — the provenance note simply
records that it is now seen. Reserves are an accelerant, never a
prerequisite; the rolling forward record is always available and never runs
out.
