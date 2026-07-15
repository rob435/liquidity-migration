# Research Summary

Updated: 2026-07-15.

This is the durable decision log. Live operational state is in `STATE.md`;
dated experiment contracts are indexed in `docs/preregistration/INDEX.md`.

## Evidence model

- This file is a decision log, not policy. Apply `docs/governance.md` to each
  claim and inspect the underlying artifacts.
- Forward demo/paper is strongest for execution behavior and is prospective
  performance evidence only within an unspent, frozen evaluation epoch.
- Venue count, metrics, and thresholds follow the claim and its registered
  contract. Cross-venue agreement is useful robustness evidence, not automatic
  independence or a universal gate.
- PIT membership, causal availability, survivorship control, material
  costs/funding, reconstructable artifacts, and data/code/config identity are
  part of any claim for which they matter.
- Historical `rejected` or `closed` decisions are current evidence states, not
  permanent bans. Revisit only with a new mechanism, new data, or a corrected
  defect—and record the new exposure.
- Mainnet is outside the current operating mode and requires separate owner
  authorization.

## Active objects

| Object | Role | Current read |
| --- | --- | --- |
| `continuous_ensemble_v2` | Bybit continuous fade demo/paper | Base stays on; sniper retired. The 5/5 TAC/SKL/VELVET result is a dated sleeve-projection receipt, not account-owner acceptance. |
| `LongV11aDivWeekendVol` | Bybit long demo/paper | Strong internal cross-venue object; TP-tail dependent; currently flat after a tiny skewed forward sample |

Binance is a research/replay venue, not a live execution venue.

## Continuous v2

### Frozen target

- Clock: `2026-06-18T19:54:00Z`.
- Components: p3 `1/3`, p4p3 `2/9`, p4p5 `4/9`.
- Entry/sizing: stable causal rmom q25, inverse vol (`target=0.01`, clamp `2`),
  prior-day BTC uptrend, `CTRL_BTC_RISK_70_90_35`.
- Capacity: 25 active shorts, 5 new entries per cycle.
- Portfolio: BTC+ETH hedge and BTC-vol regime; daily rebalance off.
- Exit: component TP12 plus 24-hour max hold.
- Removed from the future runtime: the adverse-limit add-on. Off: fixed/server
  stop, left-decile, stop-approach, failed-fade, breakeven, re-entry cooldown,
  heat and account-drawdown overlays.

The profile hash remains
`c4eb2eed1658697aa1239afd847e0de9d04f87ffe98080d4607ea6c1fd86a4f6`.

### 2026-07-10 forward incident

Six 1000TAGUSDT short legs—three base and three demo-only sniper adds—closed
for account-authoritative Bybit Closed-PnL of `-$87.69678926` (0.873502% of
`$10,039.6785` entry equity).

| Layer | Base | Sniper | Total |
| --- | ---: | ---: | ---: |
| Price PnL | -$72.44380000 | -$15.10110000 | -$87.54490000 |
| Fees | -$0.29927414 | -$0.05532786 | -$0.35460200 |
| Before funding | -$72.74307414 | -$15.15642786 | -$87.89950200 |
| Six funding credits | — | — | +$0.20271274 |

The base/sniper split is execution-attributed; funding is account/symbol-level.
The old local sniper price-PnL is not authoritative because shared exit
attribution was wrong. See `docs/incidents/2026-07-10-1000tag.md`.

Decision at the incident date: retire sniper and keep legacy cleanup active.
For the 2026-07-13 target-only cutover, that cleanup was removed only after the
new account owner gained a code-level startup gate for venue-flat positions and
zero regular/conditional orders on an empty journal. Historical decoding and
ledger attribution remain. Do not infer that a fixed stop is now positive:
20%/40%/80% fixed-stop portfolio replays reduced MAR on both venues. The
justified research response is ex-ante loss budgeting plus a separately
executable, granular adverse-state study.

### Main baseline

The 2026-06-27 frozen TP12 + BTC-risk sizing + BTC/ETH hedge replay produced:

| Venue | Return | MAR | Max DD |
| --- | ---: | ---: | ---: |
| Bybit | +26.64% | 7.33 | -1.13% |
| Binance | +18.84% | 5.72 | -1.02% |

Label: `exploratory`. It is a stable research control, not live-size approval.

The refreshed 2026-07-03 no-TP comparison kept raw survival but reduced risk
quality:

| Venue | TP12 return / MAR / DD | No-TP return / MAR / DD |
| --- | --- | --- |
| Bybit | +24.63% / 6.33 / -1.20% | +25.55% / 5.78 / -1.36% |
| Binance | +18.82% / 5.68 / -1.02% | +18.46% / 4.61 / -1.23% |

Keep TP12. Binance funding was partial, so this is survival/mechanism evidence.

The 2026-07-10 operational refresh reran the exact Bybit TP12 + BTC-risk object
through `2026-07-10` exclusive on stable-only RMOM and fully modeled funding:
+24.36% return, -1.20% max drawdown, MAR 6.22 at 1x. Label: `exploratory`.
This refresh exists to rebuild the live hedge beta tape; it is not promotion or
new alpha evidence.

The replacement is intentionally non-equivalent to the old tape. Stable-only
RMOM changed historical membership after the July 3 artifact: the pre-fix TP12
overlap has 44.3 bps maximum / 0.885 bps mean daily unit-return drift, while the
obsolete deployed TP10 overlap has 43.5 bps maximum / 5.46 bps mean drift. The
live sizing impact was small at the then-current 1.55% gross book: `$3.12` BTC +
`$0.88` ETH became `$4.12` BTC + `$0.00` ETH, below the then-active `$25`
per-leg floor. On 2026-07-12 that arbitrary strategy-side floor was removed;
current execution defers to the live symbol quantity and notional filters. The
override is recorded as a correctness migration, not parity.

### Decisions retained from closed arcs

| Mechanism | Durable read |
| --- | --- |
| 20% / 40% / 80% fixed stops | Rejected: all three trailed no-stop MAR on both venues. |
| +1h/+2h entry delay | Rejected by full component+hedge replay on both venues. |
| +1% adverse-limit entry | Promising path diagnostic, rejected by full replay. |
| Daily volatility rebalance | Keep off; it mostly saturated leverage and worsened the registered risk metrics. |
| BTC gate off / non-30d retunes | Rejected; the 30d prior-day control remains the comparison object. |
| BTC-risk 35% tail hard skip | Rejected by the two-venue rule: Binance improved, Bybit MAR/DD worsened. |
| Conditional scale-in | Raised return but worsened MAR/DD on both full overlays; no live add-on. Runtime/shadow implementation removed 2026-07-13; this decision record remains. |
| Signal-invalidation exits | Negative or zero-hit on sparse state; no deployed exit. |
| Upper-wick sizing | Retracted after duplicate-counting/parity audit. Flag-off runtime plumbing removed 2026-07-13; this decision record remains. |
| Symbol/time blacklist plan | Rejected; no deployable common arm. |

Synthetic squeeze, outage, and cluster-bootstrap diagnostics say the sampled
tiny book is survivable, but repeated worst-cluster weighting is fragile. The
loss-at-disaster diagnostic is more actionable: at a +100% shock and 0.10%
equity loss budget, about 97% of historical component trades were oversized.
That is the basis for the registered budget study—not a claim that a fillable
stop exists after a gap.

### Open continuous experiments

1. `continuous-tail-survival-2026-07-10.md`: control plus ex-ante 0.10%, 0.15%,
   and 0.25% +100%-loss budgets. Both venues and the full four-cell matrix are
   mandatory. Signals end 2026-07-10 exclusive; exit data ends 2026-07-12
   exclusive. Root receipts are byte-bound. No heavy run has executed.
2. `continuous-granular-adverse-risk-2026-07-10.md`: a separate, causal
   sub-hour adverse-state experiment with common entry timing, sequential
   one-intervention risk sets, frequency-matched nulls, and strict granular
   readiness. No treatment run has executed.
3. BTC month-regime work remains preregistered but defaults are unchanged. The
   first bare hourly-30d arm was worse on both venues, so any continuation needs
   a newly frozen confirmation/hysteresis mechanism.

## Long v11a

Latest internal cross-venue refresh through 2026-06-23:

| Venue | Trades | Return | Max DD | Sharpe-like |
| --- | ---: | ---: | ---: | ---: |
| Bybit | 188 | +32.87% | -3.46% | 1.98 |
| Binance | 190 | +27.59% | -4.00% | 1.46 |

Supporting checks:

- positive after best-month removal and 2x/3x cost stress;
- positive deterministic monthly bootstrap p05 and worst 12-month windows;
- 24/26 active-month sign agreement;
- 144/146 paired-trade sign agreement, return correlation 0.9679;
- matched random-symbol null beaten on both venues;
- PIT OHLC paths mechanically support recorded exits under the frozen ordering.

Material dependency: removing the take-profit exit bucket flips Bybit/Binance
to -0.92%/-5.99%. The object therefore needs forward fill/exit/funding evidence;
its internal result is not permission to expand size or mode.

The current ADA forward pair matched the signal but showed roughly 9.47 hours
of entry skew and 34,091.786 seconds of exit skew. That is a reconciliation
failure to learn from, not strategy validation.

## Data, reconciliation, and operations

The paper/demo counts below are historical receipts from the retired
sleeve-local reconciliation surface. They remain useful for reconstructing the
2026-07-10 decisions, but they do not validate the target-only account owner and
must not be presented as current runtime readiness.

- Latest pre-reset CONTINUOUS: paper 12, demo base 9, paired 7, paper-only 5,
  demo-only 2, sniper-only 4, open sniper 0, one exit-reason divergence.
- Latest pre-reset LONG: one paired ADA entry and no unmatched entries, with the
  timing/exit skew above.
- Venue independently confirmed flat/no-orders immediately after the incident.
  As of `2026-07-10T08:48Z`, the new clock holds TACUSDT p3 plus SKLUSDT p3,
  p4p3, and p4p5: four demo rows and four paper rows, no sniper. VELVETUSDT p3
  subsequently opened and is included in the 5/5 reconcile below.
- The dated execution compatibility report at `2026-07-10T11:46Z` was 5/5 paired, zero
  paper/demo-only, zero status or exit-reason divergence, and 72.13 bps mean,
  136.96 bps median, and 170.73 bps worst adverse demo entry slippage. The
  favorable VELVET row drags down the mean. TAC is D9 on the replayed live plane.
  SKL is D8 on that
  later snapshot (soft boundary noise), while the fresh independent full-PIT
  plane confirms it; neither plane reports a hard D7-or-lower miss.
- The current-tail PIT manifest and 1h bars cover the latest fully closed signal
  day. The LONG agreement replay is `exploratory`: full-PIT passed, but funding
  is partial and the single model entry has no live counterpart; there are no
  live entries outside the model.
- The retired report separated component legs, local price PnL, recorded fees,
  venue Closed-PnL allocations, and unavailable funding. Its compatibility
  projections are not authoritative position/P&L state under the account-owner
  design. Unknown/failed PIT remains invalid for a LONG model claim.
- Stable residual momentum now has explicit provisional provenance and exact
  schema/duplicate/non-finite gates; consumers use stable rows only.
- Current roots are not granular-ready. Bybit lacks canonical current-root 5m;
  Binance granular files are legacy/stale before the current PIT tail. The old
  2026-06-27 validation artifact must not be cited as current canonical data.
- The strict seven-day audit measured 4,288 Bybit and 5,292 Binance PIT
  symbol-days. Bybit 5m is missing and funding completeness is 14.16%; Binance
  5m/metrics have no complete current-window days, bookDepth is missing, and
  funding/OI/premium/taker-flow completeness is
  83.79%/79.48%/83.79%/83.31%. No granular treatment run is ready.
- Bybit forward depth/liquidation collectors are useful shadow context. They do
  not backfill historical causal features.
- The former hedge tape ended `2026-05-23` and left risk-increasing hedge plans
  unavailable. The current TP12/stable-only tape has 200 observations and a
  validated `2026-07-09` data boundary. Target reconciliation now runs every
  five minutes; stale non-flat state fails and pages even below the resize
  floor. Tape freshness is its validated data boundary, not merely its latest
  nonzero strategy-return date.
- The hedge is beta protection, not adverse single-name exit protection. Its
  repair does not answer the 1000TAGUSDT tail problem and does not weaken the
  requirement for the registered loss-budget/granular evidence.
- Routine work goes through `scripts/ops.sh`; ledger reset is dry-run by default
  and checked deploy requires explicit `--execute`.
- Safety release `77bf04304` produced the dated receipt summarized above. The
  pre-fix ledgers were archived with verified SHA-256; the immediate post-reset
  compatibility report was 0/0 and the later TAC/SKL/VELVET report was 5/5 with
  no hard model drift. Current deployed state belongs in `STATE.md`; neither
  receipt is account-owner acceptance. Each net venue symbol in that release had
  a TP and every component had a durable 24-hour deadline.
  Server stops remain off because the tested fixed-stop arms were negative;
  this leaves tail risk and is why the registered loss-budget and granular
  adverse-state studies remain the next evidence path.

## Account-owner cutover evidence

V4 is closed after one real `0.002 BTC` fill exposed bounded reconciliation and
competing-ACK races. V5's fresh-epoch first BTC round trip proved those repairs:
both `0.002 BTC` fills and their fees reached the immutable journal, which
recorded provisional net P&L of `-0.13755984 USDT`. V5 then exposed a distinct
ordering defect: component-owner removal for an accepted zero target can
precede its canonical reduce-only fill, and native protection misclassified
that in-flight close as an orphan. The close filled and final self-hashed
evidence proved local/venue flat with no open order. Both epochs are spent;
paper and ordinary producers never started.

V6 proved that retained-stop rule across four real closes, then its event-9 ETH
open exposed a separate exact-health publication mismatch. Reconciliation
journals every two seconds while unchanged health had published every five, so
the next gate remained one head behind (`health=201`, `journal=202`) for its
retry bound. A separate canonical recovery flattened ETH, and final evidence
bound zero local/venue exposure to an exact stopped health/journal head at
sequence 367. V6 is also spent; paper and ordinary producers never started.

V7 is now closed and spent. Its fixed 30 transitions (15 round trips) completed
and a `+0.002 BTC` funding hold opened, but the final zero remained pending because
strict reduction admission repeatedly saw reconciliation truth roughly 9--20
seconds old against the unchanged four-second limit. A separately labelled
canonical recovery target flattened BTC. The recovery owner then failed closed
when concurrent REST redelivery proposed changed immutable Close content. Final
proof retained journal integrity at 6,804 events and established zero local and
venue position/order plus no active unit. V7 ran on old host commit `98b3916`,
not the later candidate-gated `c7d6509`, so it is negative operational evidence
only and cannot support either identity. Paper and ordinary producers never
started.

Prospective V8 is a corrected-defect study on a new candidate, forward-time
surface, and six-root epoch. It timestamps direct position truth after the REST
response, runs funding recovery before that observation, reuses verified
journal/read-only state caches, and finalizes each terminal reduce-batch Close
and P&L in one serialized transaction. The two-second/four-second freshness
rule and every V7 sample, size, clock, smoke, partial-fill, and abort threshold
remain unchanged. V7 observations and recovery are excluded from every V8
floor. A passing V8 supplies the calibration needed for the deterministic paper
owner. It may also seed a later natural holdout, but that 120-hour raw-tape study
is now optional research rather than an operational prerequisite. Actual
LONG/CONTINUOUS natural tapes, sufficiency, out-of-sample twin drift,
deterministic replay comparison, venue accounting, funding, and final flatness
remain separate research gates and are not inferred from operation.

The local candidate now contains source paths for exact-candidate freezing,
natural runtime/config binding, periodic clock evidence, deterministic replay,
structural parity, sufficiency, execution-twin drift, stopped-source sealing,
fresh deployment roots, and authority-bound startup verification. The current
provenance contracts are target-replay manifest v2, event parity v3,
captured-account replay v3, comparison scope v3, kernel receipt v4, natural
sufficiency v3, and authority aggregate v4. The aggregate reconstructs exact
stopped-tree path/hash membership, reopens the dependency chain, and checks
derived-output separation; the former generic provenance implementation blocker
is therefore resolved in source.

Candidate `c7d6509` passed its clean local and non-contacting exact-head Linux
gates, but the V7 failures exposed code paths it did not repair; it is therefore
spent as the cutover candidate and was never installed. This is implementation
history, not a result. Replacement candidate `54536f1` repaired the V7 paths and
passed its clean local, canonical pre-push, and exact-head Linux gates, but its
first public-demo capacity diagnostic failed before output on a noncanonical
ticker-only Bybit row absent from the complete instrument snapshot. That
candidate was not installed or retried and is also spent. A prospective
schema-v2 source-partition repair retains and hashes such raw rows, records them
as excluded from candidate evaluation, and keeps instrument and duplicate
validation fail-closed. Its first repaired diagnostic observed 616 candidate
symbols and one rejected ticker-only row; that snapshot is capacity-planning
evidence only, not the natural freeze or a rule-coverage result. Schema-v2
candidate `344cd72` passed its registered local Ruff/full-pytest gate with 2,956
tests, then its canonical pre-push gate failed before network update because the
tracked hook placed pytest output below `.git/tmp`. Nine existing
Strategy-Overhaul source-snapshot tests correctly refused that repository-local
output (2,947 passed, one failed, eight setup errors). No alpha code or rule was
changed; the prospective hook repair uses and validates an external basetemp.
Candidate `344cd72` was not pushed or installed and is spent. There is still no
clean replacement-candidate CI receipt after that repair, passing V8 or
partial-fill gate, second reset, owner-first readiness pair, 120-hour natural
tape, venue-accounting/final-flatness receipt, stopped seal, replay result,
fresh epoch, or deployment authorization. Local
analysis timestamps enforce declared internal chronology rather than
authenticate wall-clock execution. The target manifest assigns its completion
time after replay construction; source reopening and exact dependency hashes,
not that timestamp alone, carry the causal provenance. The required order is V8
archive (through the compatibility `v7-archive` surface) before the second
reset; paper owner alone, then demo owner alone,
before producers; the fixed 120-hour demo capture; venue accounting/final
flatness and stopped sealing; only then offline replay and analysis in a
separate derived-evidence root outside all 11 sealed paths and all 10 later
deployment roots. The deterministic scheduling claim covers the registered
active LONG/CONT natural market-order paths, not every timer, historical mode,
or adverse-limit loop. No cutover branch may become `main`, be called research-
promoted, or be deleted as housekeeping before every registered promotion gate
passes. The separate Strategy Overhaul master run on the big PC is neither an
input nor an exception.

The owner subsequently separated operational retention from research capture
before V8 or any natural result was seen. Candidate `0f05060` passed its 2,957-
test local/pre-push gates, then its single exact-head Linux run retained one
filesystem-order-dependent test failure and 2,956 passes. It was not retried
and is spent; the prospective test fix selects exact dated files. Bulk raw L2
and public-trade retention is now explicit and required (`1`) for V8/natural
evidence, but disabled (`0`) for permanent demo/paper operation. Live L2,
bounded same-generation readiness, exact decision books, and account journals
remain mandatory. The five-day natural contract is deferred, unrun optional
research; this is not a positive result or promotion. Exact-branch VPS
operation is separately owner-authorized only after a new complete candidate
gate, unchanged V8 pass for paper, verified flatness, and machine/commit/input-
bound operational authority. `main`, mainnet, and real money remain
unauthorized.

## Strategy-overhaul scout status

The current claim is only that the proposed population/label plumbing can be
made fail-closed and outcome blind before touching the real roots. Validity is
limited to focused synthetic software checks; study mode is exploratory, no
population outcome has been inspected, and deployment/authorization are
unchanged.

| Surface | Synthetic hardening now present | Unresolved boundary |
| --- | --- | --- |
| CONTINUOUS raw/S02 | Gap-safe raw-history segments; exact 196-field S02 diagnostic projection; canonical sorted source/expected-population JSONL plus strict config/root/PIT/manifest-pair/map receipt binding; S02 accepts only the full-verifier result; stable RMOM causal-computability time derived as `D - 1 day + 1 hour`; provisional rows unavailable | No real population artifact has run; supplied-root/PIT completeness/authenticity and RMOM source-day/provisional-state provenance remain unproved; actual historical publication, ingestion, and operational latency are not claimed |
| CONTINUOUS S03/S04 | Separate exact typed entry-anchor and minimal path projections; anchor tamper/parity and gap/completeness checks | No real entry or label artifact has run |
| LONG S02 | Exact 138-field projection; exact runtime v11a config; canonical key; full-verifier-only population/age with exact runtime PIT/map binding; mechanically reconstructed causal availability/regime/month sidecars; reconstructed rank metadata; post-signal invariance | No real population/S02 artifact has run; authenticated root/PIT completeness and raw-hourly provenance for the context sidecars remain unproved |
| LONG S03/S04 | Separate exact 30-field entry and 71-field label projections; finite dependencies; geometry reconstruction; frozen horizons; future-bar invariance | No real entry or label artifact has run |
| Shared contracts | Central exact order/dtype/non-null/key projector; proposed registry v4; mechanically derived config bundle; 11/11 consumer-owned config validators; internally replayable source/selected-environment identity; venue-swap and physical-root-alias refusal; non-authoritative `BYTE_SNAPSHOT_ONLY` root precursor; generic byte bindings plus a separate Parquet/Arrow S02-S04 semantic verifier for current registry/scope/config/population/selected invariants/transitive parents | Internal replay is not source/environment authentication; source labels and unsigned root/PIT receipts remain untrusted lineage inputs; semantic verification does not rederive canonical IDs from the map, recompute every feature/label, or prove outcome-blind construction; no real semantic chain exists; six blockers remain; the new paths are all-in-memory and the population bundle is only atomic per file |

The remaining blocking debts are authenticated RMOM source-day/provisional-state
and root/PIT provenance; a complete independently inventoried supplied
population; canonical-ID rederivation in semantic verification; and transitive
source recomputation/binding of the mechanically derived LONG sidecars in a real
chain. Consumer-owned config checks, canonical expected-population artifacts,
and selected stage-specific semantic verification now exist prospectively, but
the generic byte receipts cannot clear the remaining debts and no real semantic
receipt has run. One outcome-blind Phase-0 inventory has now run on the
local workstation roots: bundle
`strategy-overhaul-phase0-bccefdfc38ae9fda3c17`, receipt SHA-256
`ed5fb3687280db691dcda5e32e00005a8dd48dd2fb403c2f48fe6cb69a81bb03`,
status `NOT_READY`. Strict internal re-execution returned successfully; this
proves internal reconstruction under the captured source/environment limits,
not source authenticity, full environment identity, root completeness, or
canonical lineage. The run read no OHLCV/RMOM numeric values or outcomes and
authorized no downstream action. No real S02 feature tape, S03 entry artifact,
S04 label artifact, return, MFE/MAE, PnL, or other outcome analysis has run.
This diagnostic therefore supports no gate, alpha, promotion, sizing, or
deployment conclusion.

## Current research direction

1. Accumulate enough paired forward trades on the clean post-fix clock to
   measure fills, latency, fees,
   funding, and lifecycle—not just signal agreement.
2. Run the frozen budget matrix on the larger machine only after the fixed data
   boundary and full-PIT receipts are ready.
3. Build/audit granular roots before the adverse-state experiment. Missing 5m
   data stays missing; never synthesize it from hourly bars.
4. Use the local `NOT_READY` Phase-0 bundle only to repair readiness. It found
   Binance missing 2026-07-03..09 manifest/kline partitions, 471,321
   provenance-unknown Binance membership pairs, absent Binance RMOM
   `is_provisional`, 360 Bybit kline rows without a source label, no canonical
   root-lineage receipts, incomplete auto-map consumption, and `UNWIRED` S02
   config parity in that exact historical bundle. Current source now derives
   `WIRED` 11/11 prospectively, but a new Phase-0 identity and real population/
   semantic artifacts are required before that can affect readiness. It also
   exposed a prospective code defect: two legitimate
   Bybit kline source labels were omitted from the venue sanity registry. Fix
   code and roots, produce a new big-PC Phase-0 identity, and resolve the
   semantic provenance debts before instantiating canonical children.
   Conditional association still cannot justify removing a gate.
   The local Binance daily tail has since been repaired and provenance-enriched,
   which intentionally makes the historical receipt stale for the current local
   root; a new Phase-0 identity must confirm the repair.
5. Treat closed arcs as priors against repeating the same search. Reopen a
   precise claim only with new data, a corrected defect, or a genuinely new
   falsifiable mechanism, and register the new search surface.
