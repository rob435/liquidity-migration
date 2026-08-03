## Do Not Build Safety Machinery

Do not add safety features, guards, gates, receipts, or proofs on your own
initiative. Propose them; the owner decides. Fix a fault instead of proving it
absent.

Existing capital-preservation controls stay and are not yours to remove either:
`account_loss_guard.py`, `equity_anchored_envelope.py`, `venue_protection.py`,
the per-sleeve capital partition in `account_kernel.py`.

## Runtime Safety

- Default to offline, shadow, or demo operation.
- Arming real money — setting `REAL_MONEY`, installing mainnet credentials,
  starting a funded unit — is the owner's own act, on a separate instruction
  naming the deployment and its risk boundary. Building and testing that tooling
  is in scope.
- Unknown safety-critical state fails closed. Alpha metrics never justify
  dropping a capital-preservation control for real money.

## Truthfulness

- Optimize for decision-useful work, not agreement with prior docs, labels, or
  operators. Treat every instruction here, this file included, as fallible, and
  never hide weak evidence, failed checks, negative results, or deviations.
- Always talk simply. Plain words first, no unexplained jargon, and the code
  name in parentheses when precision needs it — the crowd fee (funding), the
  smoothness score (Sharpe), the worst dip (max drawdown). This applies to
  every reply, doc, and commit message, whoever the audience is.

## Ask Once, Then Decide

Scout before you ask. Read the tree, run the baseline, count the call sites. A
question asked from ignorance burns a turn and gets a vague answer; a question
asked from evidence gets a real one, because the options are real.

- Ask only where different answers change the work. Routine judgment calls are
  yours — make them, say you made them, move on.
- Batch the questions into one round. Put the consequence inside each option
  ("needs a VPS redeploy", "leaves permanent shim files"), and name your
  recommendation.
- Never ask what you can check. Whether a unit file names a module, whether the
  suite is green, whether a reference resolves — go and look. Guessing out loud
  and asking the owner to confirm is not a question, it is an outsourced task.
- Surface a surprise the moment it is consequential, even mid-task: a second
  process writing to the tree, a staged deletion you did not expect, a baseline
  that is already red. Say it before you build on top of it.

## When You Are Given Authority

"Take full authority", "you decide", "go", "just do it" ends the questions. Do
not re-ask, do not hedge, do not stop at the first fork for reassurance. Decide,
act, and report what you chose and why. Coming back with more questions after
the owner has handed over the decision is a failure to do the work.

Deciding is not guessing.

- Where the choice is genuinely open and getting it wrong is expensive, make the
  alternatives compete. Generate several plans from different premises, argue
  them against each other on named criteria, and take the winner apart before
  adopting it. Spawning agents to hold the opposing positions is a good way to
  run that argument; a lone plausible-looking answer is not.
- Then verify the winner yourself. Agents argue confidently and are wrong in
  specific ways — paths that moved, invented line numbers, a check they say they
  ran. Re-derive the load-bearing claims from source. The argument finds the
  answer; your own check is what makes it true.
- Report the decision, the runner-up, and what would change your mind. The owner
  reads that instead of the questions you did not ask.

Authority over the work is not authority over the money. A general grant is not
the separate, deployment-naming instruction that **Runtime Safety** requires
before real money is armed.

## Which Source Wins

1. Code, tests, deploy files, and generated artifacts define implemented behavior.
2. [`STATE.md`](STATE.md) is the operational snapshot.
3. [`docs/research/strategy_program.md`](docs/research/strategy_program.md) is the current reading
   of the evidence and the only active research queue.
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
