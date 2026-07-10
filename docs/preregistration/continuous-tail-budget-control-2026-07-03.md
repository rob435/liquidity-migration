# Pre-Registration: Continuous Tail-Budget Control

Date: 2026-07-03
Stage: superseded, unexecuted; conceptual rejection withdrawn 2026-07-10
Run label until proven otherwise: exploratory

## Change

Test a tail-budgeted sizing and de-risking layer for the continuous fade book
instead of adding another fixed price stop or removing TP12.

## Why This Is The Right Method To Test

The current repo evidence says the hard part is not a missing stop level:

- TP12 plus 24h max hold remains the active lifecycle.
- Removing the hard TP survives historically, but worsens risk-adjusted quality,
  especially on Binance.
- Fixed 20%/40%/80% stops trailed the no-stop baseline on both venues.
- BTC-tail hard skip, added delay, adverse-limit entry, daily rebalance, sparse
  signal-invalidation exits, and scale-in variants are already rejected or
  insufficient.
- Existing disaster diagnostics survive at tiny size, but the disaster-loss
  sizing audit says most component trades exceed a strict +100% adverse-move
  budget.

The external research and practitioner check point the same direction:

- [Risk-Constrained Kelly Gambling](https://arxiv.org/abs/1603.06183) frames the
  problem as growth subject to a drawdown-probability constraint, not as
  maximizing return first and hoping stops save the tail.
- [Distributional Robust Kelly Strategy](https://arxiv.org/pdf/1812.10371)
  warns that nominal Kelly is fragile under distribution uncertainty and
  motivates worst-case growth sizing.
- [Portfolio Optimization with Drawdown Constraints](https://www.cis.upenn.edu/~mkearns/finread/drawdown.pdf)
  defines CDaR as the average of the worst drawdowns and shows it can be used as
  a portfolio risk constraint.
- [Trade Sizing Techniques for Drawdown and Tail Risk Control](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2063848)
  applies volatility, EVT-CVaR, and EVT-CDaR sizing directly to trading
  strategies.
- [Tail Risk Management with Puts and Trend Following](https://arxiv.org/abs/2607.00883)
  is a current arXiv paper from 2026-07-01 that frames tail risk as allocation
  across loss mechanisms rather than instrument selection. Its put-option sleeve
  is not directly executable for this perp book, but the CVaR mandate framing is
  relevant.
- Ernie Chan's Kelly drawdown note explicitly says Kelly alone does not account
  for tail risk and adds a drawdown constraint via sub-account/risk capital
  accounting: <https://epchan.blogspot.com/2010/04/how-do-you-limit-drawdown-using-kelly.html>.
- Public X/Twitter searches were noisy and direct X pages did not expose text to
  the browser, so they are weak evidence. The recurring practitioner pattern was
  still consistent: risk of ruin, portfolio heat, drawdown step-down sizing, and
  smaller size during drawdown; do not treat X snippets as acceptance evidence.

Conclusion: the proper test is a risk-budget layer that turns expected tail loss
and drawdown state into exposure, while TP12/24h stays the alpha lifecycle.

## Hypothesis

A causal tail-budgeted sizing layer can improve return per unit of drawdown/tail
loss by reducing exposure when the book's disaster loss, drawdown state, or
cluster heat is high. It should reduce CDaR95, daily/cluster expected shortfall,
shock loss, and risk-of-ruin metrics more than it reduces return. If it only
lowers headline return with no tail improvement, or raises return by increasing
hidden tail exposure, reject it.

## Method

### Layer 1: Per-Trade Loss-At-Disaster Cap

For each candidate entry, compute a disaster loss proxy:

```text
trade_disaster_loss_frac = proposed_notional_frac * shock_frac
```

The first registered shock is `shock_frac = 1.00`, matching the existing +100%
adverse-move diagnostics. Later work may replace this with a causal symbol/state
tail estimate, but not in this first run.

Candidate notional is clamped so:

```text
trade_disaster_loss_frac <= per_trade_budget
```

Registered budgets:

- `trade_disaster_010`: 0.10% equity per trade under +100%.
- `trade_disaster_015`: 0.15% equity per trade under +100%.
- `trade_disaster_025`: 0.25% equity per trade under +100%.

### Layer 2: Portfolio Heat Cap

At each entry decision, compute active non-hedge disaster heat:

```text
portfolio_heat = sum(open_non_hedge_notional_frac * shock_frac)
```

Block or downsize new entries when adding them would breach the cap.

Registered caps:

- `heat_020`: 2.0% equity.
- `heat_035`: 3.5% equity.
- `heat_050`: 5.0% equity.

The existing submit-mode cap defaults to 5% under +100%, but dry-run/paper
evidence is not clamped. This experiment must replay the same clamp in research
so the evidence and runtime semantics can be compared.

### Layer 3: Drawdown Step-Down

Apply a book-level exposure multiplier based only on prior equity high-water
drawdown:

```text
drawdown_mult = max(0.25, 1 - step_k * abs(prior_drawdown))
```

Registered arms:

- `dd_none`: no drawdown step-down.
- `dd_2x`: reduce exposure twice as fast as drawdown, capped at 25% minimum.

This is a risk governor, not alpha. It should pause adding exposure during
system underperformance; it must not be tuned after looking at the result.

### Layer 4: Disaster De-Risk Event

This first run should not introduce a symbol-level fixed stop. Test only a
portfolio-level de-risk rule:

- If prior daily drawdown is worse than -2.0% or current projected heat is above
  cap, no new entries.
- Optional flattening is measurement-only in this stage: compute the counterfactual
  flatten loss and timestamp, but do not let it choose the winner unless a later
  preregistration promotes a flatten policy.

## Data

- Venues: Bybit and Binance.
- Root requirement: full PIT universe roots; no current-universe run may enter
  the verdict table.
- Window: 2023-04-01 through the latest fully closed signal day available at
  dispatch.
- Control: current continuous TP12, inverse-vol sizing, BTC-risk overlay,
  BTC/ETH hedge, BTC-vol regime, max active/new limits, and 24h max hold.
- Costs: retain existing fees, slippage assumptions, hedge costs, and funding
  mode. Binance partial-funding rows must be labelled if still present.
- Existing usable artifacts include the 2026-07-03 TP12/no-TP equity outputs
  under `/Users/jhbvdnsbkvnsd/SHARED_DATA/tail_no_tp_2026-07-03/`. The older
  `research/continuous_fade/runs/continuous_ensemble_v2_baseline_current/` path
  is not present in the current worktree and must not be assumed.

## Cells

Run the Cartesian product only if the dispatcher can checkpoint per venue/cell:

- Control: `control_tp12`.
- Per-trade budgets: `0.10%`, `0.15%`, `0.25%`.
- Portfolio heat caps: `2.0%`, `3.5%`, `5.0%`.
- Drawdown step: `none`, `2x`.

That is 1 control plus 18 treatment cells per venue. Do not add more arms after
seeing the first result. If runtime is too high, run the same predeclared cells
in staged batches and mark incomplete cells as incomplete, not failed.

## Metrics

Report, per venue and pooled:

- Total return, annualized return, max drawdown, MAR, Sharpe, worst day.
- CDaR95 of the daily equity curve.
- Daily ES95 and ES99 of daily basket returns.
- Cluster-bootstrap p(DD >= 10%) using the existing same-signal-cluster method.
- Shock loss under one-name +100%, three-name +50%, and one-hour outage surcharge.
- Trade count, skipped/downscaled entries, average notional multiplier, and
  count of heat/drawdown blocks.
- Funding mode and full-PIT pass/fail.

## Decision Rule

Default verdict is reject.

Advance one cell to a follow-up implementation only if all are true:

- Full PIT passes on both venues.
- Return is positive on both venues.
- MAR is not worse than control by more than 5% on either venue.
- Max drawdown is no worse than control on both venues, or any drawdown worsening
  is smaller than 5% relative and offset by a larger CDaR95/ES improvement.
- CDaR95 improves by at least 15% on both venues.
- Daily ES99 improves by at least 10% on both venues.
- Shock loss improves by at least 20% for the one-name +100% and three-name +50%
  diagnostics.
- Cluster-bootstrap p(DD >= 10%) does not increase on either venue.
- The selected cell is not a venue split. If Bybit and Binance select different
  budgets, use the more conservative common budget or reject.

Promotion to paper/live is not allowed from this run. A pass only authorizes a
separate implementation PR plus paper/demo parity evidence.

## Command

Implement a dated dispatcher before running:

```bash
.venv/bin/python scripts/continuous_tail_budget_control_2026_07_03.py \
  --bybit-root /Users/jhbvdnsbkvnsd/SHARED_DATA/bybit_full_pit \
  --binance-root /Users/jhbvdnsbkvnsd/SHARED_DATA/binance_full_pit \
  --out /Users/jhbvdnsbkvnsd/SHARED_DATA/continuous_tail_budget_control_2026-07-03
```

The dispatcher must write per-cell ledgers, equity curves, tail metrics, config
hashes, source data identities, and a verdict summary. It must refuse to run
without both venues unless passed an explicit `--biased-diagnostic` flag, and
biased diagnostics cannot satisfy this preregistration.

## Artifacts

Expected output root:

`/Users/jhbvdnsbkvnsd/SHARED_DATA/continuous_tail_budget_control_2026-07-03/`

Expected files:

- `config.json`
- `bybit/<cell>/continuous_trades.csv`
- `bybit/<cell>/continuous_equity.csv`
- `bybit/<cell>/tail_metrics.json`
- `binance/<cell>/continuous_trades.csv`
- `binance/<cell>/continuous_equity.csv`
- `binance/<cell>/tail_metrics.json`
- `summary.csv`
- `verdict.md`

## Result

Never run. Owner review on 2026-07-05 rejected the plan by calling
loss-at-disaster sizing a fixed stop with extra steps. That equivalence is
methodologically wrong: an ex-ante notional cap prevents exposure before entry,
whereas a stop exits after an adverse move and assumes a fill. Fixed-stop
falsifiers therefore do not empirically reject ex-ante sizing. The conceptual
rejection was withdrawn on 2026-07-10. This broad 19-cell plan remains closed
and is superseded by the narrower, implemented
`continuous-tail-survival-2026-07-10.md`; it is not itself a positive result.
