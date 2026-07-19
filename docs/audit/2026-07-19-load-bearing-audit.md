# 2026-07-19 load-bearing code audit

Scope: the execution-critical core — `account_kernel.py` (journal,
transactions, accounting, risk evaluation), `account_service.py` +
`account_intent_client.py` (admission, supersession, convergence),
`account_execution_stream.py`, `bybit.py`, `bybit_execution_adapter.py`,
`protection_engine.py`, `venue_protection.py`, and the owner runner's
protection loop. Method: one manual deep-dive of the kernel (including a
2,000-trial randomized fill-accounting invariant fuzz that passed), two
adversarial sub-audits over the service and venue layers, and independent
verification of every reported finding against code and pinning tests before
any fix. Fixed in `6ed912c` (scaling) and `dea8f1c` (defects); every fix
carries a regression test. Gate: ruff clean, mypy clean, 2,088 passed / 3
skipped.

## Fixed (verified, most severe first)

1. **Owner-process kill via protection loop** — `market_ref` fails closed
   (ValueError) for gapped/crossed/empty books; the runner's component
   protection block called it unguarded inside a loop whose only handler was
   `KeyboardInterrupt`. One dropped public-WS delta while any component
   target was non-flat exited `main()`: execution, reconciliation, health
   publishing, and all protection down until manual restart. Now skips
   unusable books per cycle (the venue-native stop stays armed) and engine
   failures block health instead of the process.
2. **Supersession bypassed committed-batch replay** — a crash after kernel
   commit but before/mid venue submission, followed by a same-component
   flat, completed the committed request as "superseded". Its journaled
   commands stayed `commanded` in `working_order_ids` forever, convergence
   permanently reported `working`, and the filled part of the position
   could never be auto-closed. Committed batches now always replay.
3. **Silent pagination truncation** — four REST readers (`get_order_history`,
   `get_trade_history`, `get_closed_pnl`, `get_account_transactions`)
   returned partial rows at `max_pages` exhaustion with no error even under
   `strict=True`, and had no non-advancing-cursor guard. The funding
   reconciler advances its query window past whatever is returned, so a
   truncated settlement tail would have been permanently skipped while the
   pass reported healthy. All four now refuse incomplete results.
4. **Terminal-partial entry lost component stop/TP forever** — eligibility
   required `commands_fully_filled`, so a partially-filled-then-cancelled
   entry (live position, no working remainder) was skipped by every
   evaluate pass for the life of the position, leaving only the wide
   venue fallback stop. Terminal entries with observed fills now evaluate.
5. **Chunk float dust** — `remaining -= chunk` accumulation left binary dust
   on the final chunk (`2.7` into 1.0-chunks → `0.7000000000000002`), which
   the adapter transmits verbatim and the venue rejects as off-step,
   wedging the final chunk of a chunked close into a reject-retry loop.
   Chunks are now exact Decimal step multiples.
6. **Invalid protection fraction silently disabled protection** — a
   present-but-out-of-range `stop_loss_pct`/`take_profit_pct` (e.g. `8`
   meaning 8%) mapped to None with no signal, replacing the configured
   component stop with the much wider account fallback. Present-but-invalid
   now fails closed through blocked health (absent still means
   "no configured protection").
7. **Malformed known-command execution rows vanished** — unparseable
   qty/price on a fill for a known order was dropped with no log or health
   signal while the kernel position diverged from the venue. Now latches a
   health-blocking adoption failure.
8. **Queue-head starvation of convergence** — a deterministically failing
   head request was released and re-claimed every cycle and `converge_once`
   never ran, blocking reduce-only convergence closes for unrelated
   symbols. Convergence now runs in the failure path.
9. **Terminal-status consumer race** — WS delivery and REST recovery of the
   same terminal fact raised a duplicate-content integrity error from
   whichever committed second (differing local timestamps). The builder is
   now state-aware under the journal lock; the second commit is a no-op.
10. **Convergence health clock weaknesses** — the grace age re-armed on
    every accepted desire republication (an unchanged flat re-asserted
    inside the grace window could suppress the health trip indefinitely)
    and a future desire timestamp clamped to age zero. Added an
    unconverged-first-observed latch that republication cannot reset, and
    future timestamps fail closed.

Also fixed under the same audit (`6ed912c`): `submit_targets` copied and
rescanned the full journal per accepted batch, and the funding reconciler
rescanned the full event list every ~2s poll — both now incremental.

## Deferred with rationale

- `AccountState.target_proposals`/`executions`/`pnl` grow per-event and are
  pointer-copied per transaction (slow-burn O(history), bounded in practice
  by ledger resets). Pruning `target_proposals` would change observable
  behavior for venue-accounting and notification consumers; needs a
  designed refactor, not a hot fix.
- `cancel_order` transport retry can report a venue-successful cancel as
  failed (110001 on the retry). Only the demo rule probe uses it; the
  failure mode is a fail-closed false negative on acceptance tooling.
- Theoretical `protection:{key}:{status}` idempotency collision across two
  identical-plan trigger lifetimes — fails closed (integrity error), left
  as designed.

## Checked and cleared

Fill accounting math (fuzz-verified invariants: position, realized P&L,
fees, avg-price flips), journal crash-safety (fsync+rename ordering,
torn-tail projection repair, immutable segment identity), transact cache
coherence and duplicate-content traps, idempotent ack/fill redelivery,
strict-risk-reduction predicate (new-component smuggling, sign flips,
aggregate inflation), external-fill adoption transaction, adapter
double-submit protection (orderLinkId + 110089 probe), stop/TP sign logic,
exit-only preview re-proof inside the kernel transaction, inbox file
protocol (atomicity, tamper fail-closed, crash dedupe).
