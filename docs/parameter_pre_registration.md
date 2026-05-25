# Parameter pre-registration

Every parameter change that will touch a per-venue working dataset gets a
pre-registration entry under `docs/preregistration/` **before** the run. The
receipt is committed to git in the same PR as the code change.

This is the standard antidote to the parameter-mining (error #17), OOS reuse
(#18) and multiple-testing-denial (#19) failure modes catalogued in
[backtesting_errors_we_never_repeat.md](backtesting_errors_we_never_repeat.md).
Without it, every additional sweep on the per-venue datasets dilutes their
evidentiary value — and we have no longer have a clean internal OOS surface
to recover that value from.

## When pre-reg is mandatory

- Any new parameter sweep over the v11a long-only sleeve.
- Any change to a `volume-events` knob with an effect on per-venue backtest
  numbers.
- Any addition or alteration of a pattern (FOMO_CHASE / CAPITULATION_REBOUND /
  etc.) that touches per-venue backtest numbers.
- Any change to cost / funding / slippage assumptions.

## When pre-reg is optional

- Pure execution-layer / infra changes that do not change backtest numbers
  (WebSocket kline pool, daemon scheduling, ledger schemas, etc.). Mark the
  PR description `execution/infra: no backtest change` so reviewers can
  audit the claim quickly.
- Exploratory runs that will never be cited as evidence. Add the literal
  string `EXPLORATORY` to the pre-reg `Stage` field if you do write one;
  exploratory runs may not be cited in any promotion / deployment / "we
  found alpha" claim.

## Template

Copy `docs/preregistration/_template.md` to a dated file like
`docs/preregistration/2026-05-31-fc-sigma-mult-22.md` and fill it in
before the run.

```markdown
# Pre-registration: <change name>

**Date:** YYYY-MM-DD
**Author:** <handle>
**Stage:** proposed | run-pending | run-complete | rejected | accepted | EXPLORATORY

## What's changing
Single sentence. e.g. "Lower fc_sigma_mult from 2.5 to 2.0 on v11a."

## Hypothesis
Why this might work — specific mechanism, not "should improve Sharpe".

## Predicted direction + magnitude
- Sharpe Δ: +/- range
- Trade count Δ: +/- N
- Failure mode if hypothesis wrong: what would falsify

## Roots that will be touched
- [ ] bybit_full_pit (per-venue working dataset)
- [ ] binance_full_pit (per-venue working dataset)
- [ ] forward demo/paper (always, by virtue of being live)

## Decision rule (a priori)
"If post-run Sharpe Δ < +0.3 on either venue OR sign flips between venues, reject."

## Run command
```bash
... exact CLI ...
```

## Post-run results
(fill in after run; include report paths + commit SHA at which the runs landed)

## Verdict
accepted | rejected | inconclusive — with one-paragraph why.
```

## Honesty rules

- A pre-reg whose **predicted direction** is wrong cannot be quietly
  re-purposed as a different finding. Either reject the original hypothesis
  in this pre-reg and file a new pre-reg for the new hypothesis, or accept
  that the original was wrong and stop here.
- The pre-reg's `Decision rule` is binding. If you find yourself wanting to
  loosen the rule after seeing the result, the rule was wrong — write a
  retrospective addendum, do not silently soften it.
- The `Verdict` is committed to git. Do not delete or rewrite an
  `accepted` / `rejected` verdict.

## Cross-venue rule

For any per-venue test, both venues are touched simultaneously by default.
The two roots are not independent (Bybit and Binance share most of their top
20 perps), but a sign flip between venues is informative — it usually flags a
venue-specific microstructure effect rather than alpha.
