## Do Not Build Safety Machinery

Do not add safety features, guards, gates, receipts, or proofs on your own
initiative. Propose them; the owner decides. About 10,500 lines of ceremony that
merely demonstrated work happened were stripped on 2026-07-31 — fix a fault
instead of proving it absent.

Existing capital-preservation controls stay and are not yours to remove either:
`account_loss_guard.py`, `equity_anchored_envelope.py`, `venue_protection.py`,
the per-sleeve capital partition in `account_kernel.py`.

## Runtime Safety

- Default to offline, shadow, paper, or demo operation.
- Never set `REAL_MONEY`, never use mainnet credentials, never activate a live
  account. Those are the owner's own acts, on a separate instruction naming the
  deployment and its risk boundary.
- Unknown safety-critical state fails closed. Alpha metrics never justify
  dropping a capital-preservation control for real money.

## Truthfulness

- Optimize for decision-useful work, not agreement with prior docs, labels, or
  operators. Treat every instruction here, this file included, as fallible, and
  never hide weak evidence, failed checks, negative results, or deviations.
- Talk plain-first: plain words, code name in parentheses when precision needs
  it. [`docs/plain_english_guide.md`](docs/plain_english_guide.md) names every
  term once; keep it true in the change that makes it stale.

## Which Source Wins

1. Code, tests, deploy files, and generated artifacts define implemented behavior.
2. [`STATE.md`](STATE.md) is the operational snapshot.
3. [`docs/strategy_program.md`](docs/strategy_program.md) is the current reading
   of the evidence and the only active research queue.
4. Skills in `.codex/skills/` and `graphify-out/` are navigation aids, never
   factual authority — verify them against source.

A "promoted", "closed", or operator-directed label carries no evidentiary weight.
When sources disagree, read the primary artifact and fix the stale source.

## Evidence

- Every fill in every record here is simulated. No code in this repository has
  ever made a mainnet API call. Say so wherever performance is quoted.
- A number is real by physics, not process: causal inputs, executable economics,
  reconstructable accounting, an honest shaped-versus-graded data note. A miss
  makes the result a diagnostic.
- Explore freely on seen data; grade a committed config on the forward days that
  postdate its commit, and record the change point in a short promotion note. The
  commit is the registration — there is no separate filing step.
- Grade a rule on data it did not shape. Report every grid cell and era split.
  Put costs next to gross. Negative results are priors, not prohibitions, and
  `docs/backtesting_errors_we_never_repeat.md` is a failure-mode reference.

## Change Discipline

- Preserve unrelated work in a dirty tree. Prefer reversible changes.
- Refactors: compare discrete decisions and ledger keys exactly, continuous
  outputs with declared tolerances and matching NaN positions.
- A strategy change is not a refactor — explain and test the numerical
  difference rather than forcing equivalence, and record the change point.
