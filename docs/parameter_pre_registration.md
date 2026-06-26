# Parameter Pre-Registration

Use pre-registration to stop parameter mining. Keep it short.

## When Required

- Any serious parameter sweep over `bybit_full_pit` or `binance_full_pit`.
- Any sleeve-engine knob that changes per-venue backtest numbers.
- Any pattern, cost, funding, slippage, or fill-model change used as evidence.

## When Optional

- Pure infra or execution plumbing with no backtest-number change.
- Throwaway investigation explicitly labelled `EXPLORATORY`.

Exploratory work cannot accept a parameter, justify deployment, or claim alpha.

## Minimal Receipt

For a serious run, record this before running:

- Change: one sentence.
- Hypothesis: mechanism, not vibes.
- Data roots and date boundary.
- Decision rule: reject/accept/inconclusive thresholds.
- Exact command or script.
- Expected artifacts.
- Run label.

After the run, add:

- Artifact paths.
- Headline metrics.
- Verdict.
- Commit SHA if relevant.

Use `docs/preregistration/_template.md` if a standalone receipt is worth keeping.
Otherwise summarize the decision in `docs/preregistration/INDEX.md` and
`docs/research_summary.md`; git history is the archive.

## Honesty Rules

- Do not soften the decision rule after seeing results.
- Do not repurpose a failed hypothesis as a different win.
- Both venues matter by default; a sign flip is usually a microstructure warning,
  not a technicality.
