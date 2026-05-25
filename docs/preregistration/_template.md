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
