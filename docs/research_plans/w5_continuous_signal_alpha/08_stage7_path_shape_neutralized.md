# Stage 7 - Neutralized Path-Shape Scoring

## Question

Does the *shape of the move into a confirmed entry* carry stable, causal
information about the trade, after the symbol / component / time-of-entry mix is
removed?

This is the "Stage 3b" the W4 program promised and never ran. W4 Stage 3
measured fixed causal path-shape features but could only **nominate** them,
because the `symbol_hash_bucket` negative control showed a large 97 bps pooled
top-bottom spread — i.e. a big share of the apparent path-shape signal was
really "which symbol is this," not "what shape was the path." This stage exists
to neutralize that confound and decide the question.

## Binding W4 context

- Window: `2023-04-01 <= signal_ts < 2026-05-01` (common full-PIT overlap).
- Nominated-for-test causal features (do not use raw): `pre_6h_return`,
  `pre_24h_return`, `pre_24h_realized_vol`.
- Banned as causal filters (diagnostics only): `post_6h_adverse`,
  `post_6h_favorable` — they use post-entry path.
- Negative control to beat: `symbol_hash_bucket` (97 bps pooled spread in W4).
- Receipt to read first: `docs/preregistration/2026-06-13-w4-continuous-stage3-path-shape.md`.

## Required neutralization

A path-shape feature is admissible only after the score is residualized against
the confounds, not measured raw. Before any effect-size test, regress / bucket
out at minimum:

- symbol (and a symbol-hash bucket as the control);
- component (`turn3p3`, `turn4p3`, `turn4p5`, `age210tp14`);
- entry calendar bucket (month and session/hour-of-day);
- the existing Stage 1 composite score (path-shape must add value *beyond* the
  score already in production, not re-express it).

The tested quantity is the **residual** path-shape signal versus per-notional
net return. Report the raw effect alongside, but the decision is on the residual.

## Arms

- `P0_control`: Stage 1 composite score only, no path-shape.
- `P1_residual_pre_return`: residualized `pre_6h_return` + `pre_24h_return`.
- `P2_residual_runup_vol`: residualized `pre_24h_realized_vol`.
- `P3_residual_combined`: residualized combination of the three nominated
  features (locked weights / locked model, walk-forward only if a fit is
  unavoidable).
- `P4_negative_control`: `symbol_hash_bucket` pushed through the identical
  neutralization pipeline.

## Metrics

- Spearman IC of the residual score vs per-notional net return, per venue;
- top-bottom tercile spread (bps) of the residual score, per venue and pooled;
- the same spread by chronological thirds (fragility);
- residual score's marginal IC over the Stage 1 composite;
- coverage / missing-feature counts;
- R1 robustness on any arm that is also run through the engine for sizing/entry.

## Pass Bar (admissible to feed Stage 1 / 2 / 5)

A path-shape arm is admissible only if, on the **residual** score:

- both venues have at least 500 covered events;
- Spearman IC has the same sign on both venues;
- pooled top-bottom spread has the same sign on both venues and `>= 25` bps per
  notional;
- at least two of three chronological thirds agree in sign;
- the residual arm's absolute spread **exceeds the neutralized negative-control
  spread** (`P4`) — this is the gate W4 could not clear;
- it shows positive marginal IC over the existing composite (it is not just the
  composite relabeled).

## Falsifier

Reject path-shape as a usable feature if, after neutralization, the effect is
no larger than the hash-bucket control, flips sign across venues, lives in one
chronological third, or vanishes once the Stage 1 composite is partialled out.
A raw (un-neutralized) spread is never sufficient — W4 already showed it is
dominated by symbol mix.

## Downstream use

Only an admissible residual path-shape score may be carried forward, and only as
an input to an engine stage that still has to clear its own bar:

- Stage 1: as an additional candidate-priority term;
- Stage 2: as a predeclared bucket for entry style (`E4`);
- Stage 5: as the `Z2_path_shape_size` bucket.

Admissibility here is necessary, not sufficient. The downstream engine stage
must still beat its control on pooled MAR both venues before anything is a
demo/paper candidate.
