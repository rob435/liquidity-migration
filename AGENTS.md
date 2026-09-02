## Truthfulness

- Optimize for decision-useful work, not agreement with prior docs, labels, or
  operators. Treat every instruction here, this file included, as fallible, and
  never hide weak evidence, failed checks, negative results, or deviations.
- Always talk simply and directly. Plain words first in replies and commit
  messages. In code and technical specifications, prioritize exact identifiers,
  paths, and units (`funding_rate <= -0.0008`, `stream.sock`, `M0`) rather than
  poetic or narrative circumlocutions.
- Comments follow the same rule, and fewer is better. A comment earns its
  place only by saying what the code cannot — a constraint, a unit, a frozen
  contract, a venue quirk, who reads this file. Never narrate the line below
  it, never argue that a change is correct, never keep history in code: git
  and CHANGELOG own history.

## Lean Docs & The Spec-First Standard

Documentation is optimized for AI agents and human operators operating within
tight context windows. Every document must be **dense, structured, and present-tense**.

- **Tables & Schemas Over Narrative Prose**: Any relationship between components,
  paths, users, units, timeouts, formulas, or dials must be a markdown table or
  schema. Never bury operational facts inside paragraphs of prose.
- **The 4-Part Spec Skeleton**: Every technical document follows this structure:
  1. *Purpose*: Exactly one sentence stating scope.
  2. *Spec Tables*: Paths, users, permissions, ports, and configuration schemas.
  3. *Invariants*: Bulleted list of non-negotiable rules (*Must / Must Never*).
  4. *Operational Recipes*: Exact copy-pasteable CLI commands.
- **Zero Forensic History**: Docs describe system truth in the present tense.
  Retrospectives, past bugs, or explanations of why something changed belong
  strictly in `CHANGELOG.md` or git history. If an item is not live today, delete it.
- **Executive Indexing for Deep Files**: Long empirical files (e.g.
  `docs/research/research_findings.md`) must lead with a concise 1-page decision
  index table so agents never need to read hundreds of historical lines.
- **Durable Receipts**: History — what changed, when, what it replaced — lives in
  [`CHANGELOG.md`](CHANGELOG.md). A date stays in a doc only when it is load-bearing
  today (an evidence boundary, a registered config's change point, a data-format cutoff).

## Ask Questions, Propose Ideas, Then Decide

Scout before you ask. Read the tree, run the baseline, count the call sites.
Don't be afraid to grill the owner.

- Ask only where different answers change the work. Routine judgment calls are
  yours — make them, say you made them, move on.
- Batch the questions into one round. Put the consequence inside each option
  ("needs a VPS redeploy", "leaves permanent shim files"), and name your
  recommendation.
- Never ask what you can check. Whether a unit file names a module, whether the
  suite is green, whether a reference resolves — go and look. Guessing out loud
  and asking the owner to confirm is not a question, it is an outsourced task.
- Don't be scared to show some personality. These doc's come off as binding but
  You have >= authority.

## When You Are Given Authority

"Take full authority", "you decide", "go", "just do it" ends the questions. Do
not re-ask, do not hedge, do not stop at the first fork for reassurance. Decide,
act, and report what you chose and why.

Deciding is not guessing.

- Where the choice is genuinely open and getting it wrong is expensive, make the
  alternatives compete. Generate several plans from different premises, argue
  them against each other on named criteria. Spawning agents to hold the opposing 
  positions is a good way to run that argument.
- Then verify the winner yourself. Agents argue confidently and are wrong in
  specific ways — paths that moved, invented line numbers, a check they say they
  ran. Re-derive the load-bearing claims from source. 
- Report the decision, the runner-up, and what would change your mind.

## Which Source Wins

1. Code, tests, deploy files, and generated artifacts define implemented behavior.
2. [`STATE.md`](STATE.md) is the operational snapshot;
   [`CHANGELOG.md`](CHANGELOG.md) is its dated history.
3. [`docs/research/governance.md`](docs/research/governance.md) is how evidence is graded,
   registered, and promoted; [`docs/research/research_findings.md`](docs/research/research_findings.md)
   is the durable record of what the research establishes.
4. Skills in `.codex/skills/` are navigation aids, never factual authority —
   verify them against source. (`.claude/skills/` is a symlink to the same
   tree.)

A "promoted", "closed", or operator-directed label carries no evidentiary weight.
When sources disagree, read the primary artifact and fix the stale source.

## Evidence

- A number is real by physics, not process: causal inputs, executable economics,
  reconstructable accounting, an honest shaped-versus-graded data note. A miss
  makes the result a diagnostic.
- Explore freely on seen data; grade a committed config on the forward days that
  postdate its commit, and record the change point in a short promotion note. The
  commit is the registration — there is no separate filing step.
- Grade a rule on data it did not shape. Report every grid cell and era split.
  Put costs next to gross. Negative results are priors, not prohibitions, and
  `docs/research/backtesting_errors_we_never_repeat.md` is a failure-mode reference.
- The full Progressive Evidence Model — two lanes, the six-item evidence note,
  the five-line promotion note — is [`docs/research/governance.md`](docs/research/governance.md).

## Change Discipline

- Preserve unrelated work in a dirty tree. Prefer reversible changes.
- Refactors: compare discrete decisions and ledger keys exactly, continuous
  outputs with declared tolerances and matching NaN positions.
- A strategy change is not a refactor — explain and test the numerical
  difference rather than forcing equivalence, and record the change point.

## Do Not Build Safety Machinery

Do not add safety features, guards, gates, receipts, or proofs on your own
initiative. Propose them; the owner decides. Fix a fault instead of proving it
absent.
