# Changelog

The dated operational log: deploys, incidents, repairs, and change points,
newest first. Each entry is kept as it was written on its day, so a later
entry supersedes an earlier one — read from the top down. Current truth lives
in [STATE.md](STATE.md); when something happens, add the dated entry here and
edit STATE.md to match.

- **2026-08-26 — The demo rule-receipt freshness alert is removed (owner
  directed).** The demo receipt no longer pages `demo_rules_age`; nothing in
  the demo runtime path reads the receipt, and a demo receipt in the back half
  of its life renews itself on the next rollout, so the weekly WARNING only
  taught operators to ignore a WARNING. The funded receipt still gates the
  owner, still renews on any deploy, and still pages WARNING/CRITICAL under
  `venue_rules_age` — that gate is untouched. `check_fleet_liveness.py` now
  scopes the rules-receipt gather to mainnet only.

- **2026-08-26 — The carry rule rename: registered rule goes to `lane2_carry_hold_v7`
  (name only).** The registration that was `lane2_carry_hold_v6` becomes
  `lane2_carry_hold_v7`, so the live name and the config filename both read
  v7. Nothing about the rule, the config, the parameter values, or the
  forward grading changed — the file `configs/lane2_carry_hold_v6.json` was
  renamed to `lane2_carry_hold_v7.json` and its `config_id` updated to
  `lane2_carry_hold_v7`; `CARRY_CONFIG_PATH`, the v7 profile, and both clock
  profiles now read `lane2_carry_hold_v7.json`. The v6↔v7 id is a DATING/NAME
  change point, not an evidence one: rows graded under `lane2_carry_hold_v6`
  (through 2026-08-21) are the same rule under the old id, and the forward
  experiment differential is now `carry_hold_v7_minus_v5`. The journal keys
  (`carry_hold_v6_live_v1`, `carry_hold_v7_live_v1`) and the settled-print
  rollback dial `CARRY_STRATEGY_PROFILE=v6` are unchanged.


  🔴 lost it, only where there is a verdict — and the verdict leads: an
  exit's first line is the dot, the account, the sleeve, and the net in
  bold, because the phone's notification preview shows one line. Every
  message names its account (RM = real money, DEMO = demo), sleeves act in
  verbs (enters, shorts, exits, covers, closed), prices carry four
  significant figures, every return reads as percent of the position (never
  basis points — those stay in the engine's reports), slip reads "paid" or
  "saved" because its adverse-positive convention runs against the net
  beside it, and the daily summary
  opens with the day's own colour
  over a monospace win–loss table whose rows are per account and sleeve — so
  real money never melts into a demo figure. Messages are Telegram HTML now:
  `send_telegram_message` grows an opt-in `parse_mode` argument, opt-in
  because HTML rejects a stray `<` — the notifier escapes its text and asks
  for it, the watchdog stays plain. The notifier's state schema is unchanged,
  so the changeover run sends nothing spurious. `scripts/runtime/
  notify_book_changes.py`, `liquidity_migration/ops/telegram.py`,
  `docs/notifications.md`.

- **2026-08-24 — LLM gate prompt v7: the crime-pump playbook joins the
  rubric (owner approved).** The driver-judgment prompt
  (`scripts/research/llm_driver_ledger.py`) moves to
  `driver-judgment-v7-crime-pump`. Two changes, both judgment food, no new
  mechanical rule: (1) a new enrichment fact `turnover_to_oi_24h` — the
  day's traded volume against the standing open interest (the venue reports
  OI in contracts, so notional derives as contracts × price) — the churn
  read that public research on manufactured pumps calls "brushed" volume;
  (2) the manufactured-pump step now names the two documented crime-pump
  shapes — the low-float walk-up and the short-squeeze bait — and each
  judgment reports a `manipulation_shape` verdict. The one outside number
  (volume-to-OI low single digits typical, 20+ suspect) is labeled
  unmeasured on this desk inside the prompt itself; every measured prior in
  the rubric is unchanged. `--grade` buckets by prompt version, so v7
  accrues its own forward record and v6's rows are untouched. The entry
  gate is unchanged: score ≥ 6, same candidates file, same LONG-sleeve
  sizing, exits, and stops. Motivation: a public post-mortem of seven
  manipulated tokens (MYX, COAI et al.); its mechanical signals are already
  measured dead on this book (OI exits, funding-flip exits, pool-level
  taker reads — receipts in `docs/research/research_findings.md` §2), so
  the judged rubric is the one seam that takes it. This commit is the
  change point. Deployed `b51aa3a8` via `staged --stop-first` the same day:
  verify-ok on the commit, both engines rebuilt on it, the funded engine's
  boot reconciliation stayed clean (`may_open: true` in the mainnet
  heartbeat — false is the latch, `engine/engine-types/src/wal.rs`), and
  the ledger service's first run under v7 completed green on a quiet hour
  (0 movers, 0 triggers, so 0 rows — the first journaled
  `driver-judgment-v7-crime-pump` row is the runtime receipt to watch).
