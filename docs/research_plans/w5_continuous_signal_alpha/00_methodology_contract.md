# W5 Methodology Contract

This is the standard every W5 stage must satisfy.

## Required Timing Fields

Every event row must declare:

- `decision_ts`: timestamp at which the system makes the decision;
- `data_available_ts`: timestamp at which every feature used was available;
- `order_submit_ts`: first timestamp an order could be submitted;
- `fill_window`: exact simulated or observed fill interval;
- `exit_activation_ts`: timestamp an exit rule first became active;
- `state_initialization_ts`: timestamp from which stateful rules were warm-started.

If a field is ambiguous, stop and fix the event model. Do not hide the ambiguity
inside a report.

## Root And Universe Rules

- Both venues are mandatory.
- Full-PIT universe is mandatory.
- Current-universe diagnostics are allowed only as explicitly biased benchmarks.
- Binance and Bybit must use comparable windows. If roots differ, use the common
  overlap or write a data-gate amendment before running.

## Costs And Execution

Each stage must model or explicitly report:

- taker fees;
- maker/post-only assumption, if used;
- spread/slippage;
- funding/carry;
- resize/turnover costs;
- capacity and depth constraints where applicable;
- missed fills and opportunity cost for passive/conditional entries.

Maker rebates cannot be credited unless supported by realized demo/depth
evidence. In historical simulations, prefer conservative taker-equivalent costs
unless the stage is specifically measuring passive fills.

## Artifact Contract

Every serious stage writes:

- dated preregistration;
- raw event tape;
- selected and rejected candidate rows where relevant;
- per-venue ledgers;
- monthly return CSVs compatible with `scripts/r1_robustness.py`;
- report JSON with root identity, config hash, code hash, PIT status, and run label;
- Markdown verdict with falsifier outcome;
- R1 robustness output;
- negative-control results.

## Decision Labels

Use the lowest defensible label:

- `exploratory`: useful measurement but missing a proof gate;
- `candidate`: full-PIT, causal, costed, ledger-backed, split-stable, not tuned
  on the verdict window;
- `paper_ready`: candidate plus matched demo/paper lifecycle plan.

Nothing in W5 can be real-money evidence without the strict Tier-3 forward-demo
bar from `STATE.md`.

## Multiple Testing Controls

- Predeclare arms.
- Use walk-forward training only where a model is unavoidable.
- Keep negative controls in every stage.
- Do not move thresholds after seeing output.
- Do not convert a failed filter into a "score" by reducing trade count.
- Do not let a single venue carry the conclusion.

## Falsifier Standard

Each stage must state what makes the mechanism disappear. Examples:

- same-count score priority fails versus control;
- negative control matches or beats the proposed score;
- return sign flips on either venue;
- pooled MAR delta fails the Tier-2 bar;
- drawdown/worst-day loss worsens beyond the preregistered tolerance;
- live fill sample is too small.

## Objective Standard

The objective of every stage is **risk-adjusted return (pooled MAR) vs the
frozen control on both venues**, net of funding and costs. The following are
never objectives and may not be a stage's success metric:

- trade count, "uptime," or "the book is always trading";
- raw total return without the drawdown/MAR and both-venue checks;
- a single-venue or single-month improvement.

A change that makes the book trade more (more regimes, more names, more often)
passes only if it improves pooled MAR within the drawdown tolerance. More
activity is an allowed *side effect*, never the target.

## Regime / Closed-Mechanism Rule

E2 (`docs/preregistration/2026-06-12-e2-regime-response-family.md`) closed the
bounded-threshold "trade more regimes" family (V1 euphoria cap, V2 downtrend
quarter-size): both lost ~20pp return and roughly halved MAR on both venues. Any
regime work in W5 (Stage 8) must be **mechanistically distinct** from that
family — a continuous regime to size map, a multifactor regime, score-
conditioned admission, or regime-conditioned hedging — and must beat the V0
binary gate, not merely V1/V2. More generally: a closed mechanism may be
reopened only by a materially different form with a fresh dated preregistration.
Re-running a falsified mechanism in its exact failed shape is prohibited, even
under a "be relentless" directive.

## Research Posture

Be relentless and do not give up on finding edge. A NULL closes one hypothesis,
not the program: when a mechanism fails, bank the honest verdict and propose the
next mechanistically distinct hypothesis. Persistence lives in the *breadth and
novelty of hypotheses* — entry, exit, path-shape, regime, sizing, interaction —
run at full artifact discipline, with honest kills and forward-only promotion.
Persistence does **not** live in re-running a dead mechanism, lowering a
threshold after seeing output, or leaning on one venue. Relentless here means
"always have the next real experiment queued," never "make this one pass."
