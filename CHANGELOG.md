# Changelog

The dated operational log: deploys, incidents, repairs, and change points,
newest first. Each entry is kept as it was written on its day, so a later
entry supersedes an earlier one — read from the top down. Current truth lives
in [STATE.md](STATE.md); when something happens, add the dated entry here and
edit STATE.md to match.

- **2026-08-30 — The whole-repository audit removes silent ambiguity at the
  inputs, order path, account edge, and deploy edge.** Market numbers now
  reject NaN, infinity, non-positive prices and invalid sizes at one shared
  boundary; funding, rolling clocks, WebSocket liveness, LONG admission,
  historical entry prices, financed cash, carry re-entry, and terminal
  liquidation all have direct regression cases. The Rust adapters validate
  fill identity and quantity before changing state, start scans at the real
  subscription boundary, parse venue numbers and cursors strictly, keep empty
  execution checkpoints durable, and preserve unknown fee truth. WAL formats
  remain backward-readable. Deploys bind one fetched commit and one SSH host,
  stop children with a measured grace, join timer triggers to their service
  invocations, keep notification retries durable, and make flattening refuse
  stale or partial account evidence. A new demo-only `engine canary-order`
  takes the account lease, verifies the authenticated UID, sends one protected
  minimum PostOnly order, requires exact terminal status and venue-clock
  execution history, and reconciles any ambiguous fill through one full close.
  The Bybit archive repair recovered 1,180 of 1,454 thin or missing symbol-days;
  strict full-PIT coverage still fails on 265 listing-inferred empty days and
  nine official gzip objects that decompress to zero bytes. Those 274 rows stay
  visible and required. Dependency locks were rebuilt and matched, both Python
  and Rust advisory scans found no known vulnerability, and the complete check
  finished with Ruff, mypy, 1,792 Python tests and the full Rust workspace
  green.

- **2026-08-30 — The gate stops chasing old moves, and every LONG entry
  says where it came from.** Two changes from the give-back program's live
  receipts (research_findings §LONG give-back). First, the LLM gate's
  freshness veto: a trigger whose name the ledger already flagged on two or
  more distinct earlier UTC days within the last four is journaled in full
  (`freshness_veto`, `prior_flag_days`) and never published — the AAVE
  2026-08-24 loss was exactly this chase, scored 7 on its third consecutive
  mover-day after a +45% three-day run and bought within 2.5% of the top.
  `--grade` buckets vetoed rows separately, so the ledger carries the veto's
  own forward A/B. Second, both LONG producers append an enter/leave
  attribution line per book transition (`LONG_ENGINE_BOOK_TRANSITIONS_PATH`,
  `targets/long-{demo,mainnet}-transitions.jsonl`) carrying the entry's
  pattern (`llm_gate` vs native `fomo_chase`); since the 2026-08-24 merge no
  close record could say which entries were the gate's, and this log is the
  durable split. A failed append warns and never stops the cycle. Six new
  tests, each proved to fail without its change. This commit is the veto's
  change point. Deployed the same night via `ops.sh deploy rollout` at
  `d673b578` (verify-ok, whole topology active): the demo engine's
  `SuccessExitStatus=143` is loaded and a live restart logged `Deactivated
  successfully`; forward-capture restarted onto the near CloudFront edge at
  00:57 UTC; both heartbeats healthy with `may_open: true`. Three agents
  were operating this repo and host concurrently that night — a competing
  rollout was detected by its held maintenance lock and waited out, never
  broken.

- **2026-08-30 — The forward tape has an off-box home.** An hourly uploader
  sends only completed `.zst` segments to Google Drive, checks each new batch
  before advancing its local ledger, and leaves a SHA-256 batch list beside
  the remote files. Open `.partial` segments, account WALs, credentials and
  environment files never enter this path. The first live object matched the
  VPS copy in both size and MD5; the Drive account reported roughly 5 TB free.

- **2026-08-30 — A commanded stop is now filed as one.** Teaching the engine
  to answer SIGTERM was only half of it, and the deployed binary proved it:
  every unit runs under the trusted supervisor, which forks its workload
  rather than replacing itself with it, so bash is systemd's main process. It
  answers a stop correctly — SIGTERM to the child, wait, escalate — and then
  exits 143. systemd's default success set is `{0}`, so every `systemctl stop`
  was filed as `Failed with result 'exit-code'` and paged the alerts line on
  every deploy. The eight long-running units now carry
  `SuccessExitStatus=143`, and a posture test holds every `Type=simple` unit
  to it and to the supervisor still exiting 143.

- **2026-08-30 — Every Telegram message is one monospace block, and the
  canary is off the phone.** Builders write plain text; `as_block` escapes it
  and wraps it once at the send, so trade updates, the daily summary, watchdog
  alerts and the engine digest all arrive as a block that copies in a tap and
  keeps its columns. The prose that explained itself is gone — the funding
  caveat, the reason a close could not be priced — and lives in
  [docs/notifications.md](docs/notifications.md) where it is read once instead
  of every day. `maker_canary` exercises the order path rather than earning,
  so its closed trades now reach stdout and journald only: no message, no row,
  and no part of the day's trip count or total. A day that was reported as
  "14 trips · none won · -$10.92" reads as "2 trips · none won · -$10.74".

- **2026-08-30 — The fleet was reaching Singapore by way of another
  continent.** Bybit is served through CloudFront, which picks its edge from
  the resolver the query arrives on. The box resolved over IPv6, has no
  working IPv6 egress, and was handed an edge 206 ms away; the same resolver
  asked over IPv4 named the Singapore edge 2 ms away, minutes from the box.
  Every REST call, every market message and every order had been paying that
  detour. The box now resolves over IPv4 only
  (`/etc/netplan/99-dns-ipv4-only.yaml`) and prefers IPv4 addresses
  (`/etc/gai.conf`), and the whole fleet was restarted onto the near edge.
  Measured on the box, before against after: edge ping 206.4 ms against
  2.0 ms; a full API call including the TLS handshake 858 ms against 26 ms;
  the trade socket's connect-to-authenticated round trip 429 ms against
  3.2 ms; a producer's restart-to-first-completed-cycle 30-100 minutes
  against 11 seconds. Both engines ran on this box at the same moment on
  either side of the change, which reads the difference directly:
  `venue_clock_offset_ms` -227 on the far edge against -21 on the near one.
  `forward-capture` was left running and still holds a far-edge socket.

- **2026-08-30 — The LLM driver ledger can import its own package.** The
  wrapper runs each script by path, so Python puts the script's directory on
  the import path rather than the repo root, and the package is not installed
  into the venv. `llm_driver_ledger.py` imported `liquidity_migration` without
  first putting the root on the path, so the unit had been failing at import
  and collecting nothing. It now bootstraps the path the way the other
  dispatched scripts do, and a test holds every script the wrapper can
  dispatch to that rule.

- **2026-08-30 — A systemd stop is a stop, and a stuck strategy writes one
  note.** The engine waited on SIGINT for its shutdown, and systemd stops
  services with SIGTERM, so every deploy killed it: the log's buffered tail
  never reached the OS, the account lease was dropped by process death rather
  than by hand, and systemd recorded the clean stop as `Failed with result
  'exit-code'` on status 143, which paged the alerts line. It now waits on
  both signals and exits zero. Separately, a refusal wrote a WARN and a WAL
  note every time, so a position whose protection refused each new entry wrote
  one per quote — 3110 of them in a single episode on 2026-08-29, into the log
  the fill and latency reports read. An unchanged refusal is now recorded
  once a minute with a count of what it stood for; what the engine refuses is
  unchanged.

- **2026-08-30 — The forward tape records the fast touch.** The recorder now
  keeps Bybit's 10 ms L1 snapshots beside its L50 book, trades, ticker and
  liquidation feeds. Every book row names its depth; L1 and L50 keep separate
  update histories and retain the venue cross-sequence that orders the feeds.
  A 15-second, 34-symbol live probe measured 14.5 KB/s of added raw WebSocket
  payload against 96.3 KB/s for the existing feeds.

- **2026-08-30 — The first toxic-flow canary stopped early, and whole-position
  dust can now close.** The registered run produced 10 new attributed fills,
  all maker: 8.81 bp all-in arrival cost and -14.52 bp signed one-minute
  markout. That is adverse but far too small to grade the rule. The run was
  stopped before its 30-fill-or-60-minute boundary when it left 10 AGI that
  the normal quantity/value checks would not submit. The quoter is disabled.
  For a venue that states this capability, the engine now recognizes only an
  exact, reduce-only, market exit for the whole fresh position as a below-minimum
  close. Bybit renders that request as `qty=0`, `reduceOnly=true`, and
  `closeOnTrigger=true`; the durable request keeps the actual quantity for
  accounting. Partial dust exits and malformed full-close requests remain
  refused.

- **2026-08-30 — Execution-health telemetry reaches the live heartbeat.** It
  now states p99 disk-wait residue, p99 request-quota hold, accepted amends
  confirmed versus pulled after the venue stayed silent, private-stream resets
  including the initial subscription, and venue clock minus host clock. The
  clock sign is pinned by a direct test so a positive number means the venue
  is ahead, matching the field's words. Each Telegram-enabled scope sends one
  plain digest per UTC day from these fields and retries until delivery; its
  day marker is reserved watchdog state, not an alert cooldown. Optional host
  clock and off-box-backup-stamp checks remain off until configured.

- **2026-08-30 — Deploys stop paging the alerts line.** The night's traffic —
  ten of twelve messages — was one alert churning: "producer restarted but has
  not completed a checkable cycle", CRITICAL, on every producer after every
  deploy, clearing itself 30–100 minutes later. The diagnosis: a producer's
  first completed cycle after restart pays the boot kline backfill
  (`bootstrap_timeout_seconds` budgets 20 minutes, and four producers on one
  box contend for the same REST budget — near 100 minutes observed after a
  full-fleet deploy), while the watchdog's startup grace reused the 10-minute
  steady-state freshness dial. Warming up read as hung. The fix gives startup
  its own physics: `--max-startup-min` (default 120) covers a producer that is
  verifiably active in its current systemd generation, silently; past it the
  page now says "up N min without completing a cycle — past the startup
  budget, so this is a hang, not a warmup". A dead or failed unit never gets
  the grace — unit-state checks page those within minutes, which is what makes
  the long budget safe. The regression test encodes last night's exact spam
  shape (45 and 100 minutes into boot, current generation, no receipt → no
  alert) and fails under the old grace.

  The daily digest also gains the engine's uptime, because every counter in it
  is since-boot and "fills 0" two minutes after a deploy was reading like a
  dead day.

- **2026-08-30 — The debugging channel gets one engine-health line a day, and
  the small hygiene lands.** Built and tested; the next deploy enables it.

  The heartbeat now carries the numbers this week's execution work created:
  how long the order path actually waited for the disk (`barrier_wait_p99_ns`)
  and for the request quota (`quota_hold_p99_ns`, a new ledger segment recorded
  at every order/cancel/amend completion), amends priced by the venue against
  amends pulled unanswered, private-stream resets, and the venue clock offset
  measured off the freshest quote with both clocks sampled together. The
  heartbeat's exact-keys test pins all six.

  Each liveness unit posts one plain-text digest per UTC day on the alerts
  line, built from that heartbeat: standing, equity, fills with maker share
  and slip and markout, submit and round-trip times, the two pacing numbers,
  the amend outcomes, and the clock offset. Absent fields print as dashes,
  never as confident zeros. The day advances only on a delivered message, so
  a failed send retries next run; a broken gate in either direction is pinned
  by a main-loop test proved to fail both ways. The hourly digest stays dead —
  this is daily, and `--no-daily-digest` turns it off per scope.

  The hygiene: the demo watchdog passes `--host-clock-check` and pages when
  `timedatectl` says the clock is undisciplined (one scope per box, so one
  cause pages once); `backup_state.sh` plus a nightly timer rsync the WALs and
  trade files off-box and touch a stamp whose age the watchdog alarms on —
  armed only once `backup.env` and `LIVENESS_BACKUP_STAMP_FILE` are configured,
  so nobody is paged about a backup they never set up; and `chaos_drill.sh`
  plus a Sunday timer kill the demo engine weekly and report clean/latched/
  did-not-return on the alerts line. The drill is hardwired to the demo unit,
  a test forbids the funded unit's name from appearing in it, and its timer is
  deliberately not Persistent — a box booting after a real outage has just had
  its recovery exercised and does not need a rehearsal on top.

- **2026-08-29 — The maker protects only the side aggressive flow is attacking.**
  Public trade notional is divided by displayed same-side dollars within a
  volatility-expanded near-touch band, then carried in 250 ms and 3 s decays.
  Buying widens or pulls only the ask; selling does the same only to the bid.
  Every attributed fill records both flow states, the combined score, nearby
  depth, spread, movement, and estimated queue beside its execution id. The
  34-name, two-day paired queue replay chose four basis points of widening per
  score over the fee-corrected control: +0.076 bp per markable quote, paired t
  11.75, with the improvement present on both dates. The selected arm still
  loses -0.171 bp per quote after the full fee assumption. It is registered as
  `lane2_toxic_flow_quoter_v1` for a minimum-size 30-fill-or-60-minute funded
  trial, not promoted as profitable.

- **2026-08-29 — Forward public market capture is an owned service.** A
  no-credential unit records Bybit L50 snapshots/deltas, public trades,
  mark/index price, the crowd fee (funding), open interest and liquidations
  with both venue and local receive times. It rotates per-symbol raw segments,
  atomically installs a `zstd` copy only after decompression verification,
  writes its SHA-256 receipt, and only then removes the raw bytes. Recovery
  keeps complete JSON lines after interruption. Retention removes completed
  compressed segments after 30 days, above 60 GB, or to preserve 25 GB free;
  disk pressure counts dropped frames without traceback spam. The live smoke
  captured book, trade and ticker rows with no writer-queue drops.

- **2026-08-29 — The disk barrier runs beside the send instead of in front of
  it.** The order path waited out a full `fdatasync` — ~2.2 ms on the VPS,
  3.95 ms measured here — before a single byte left, and the fsync was
  comparable in size to the venue round trip it was blocking. It now starts at
  the same moment the order is dispatched: the bytes are with the operating
  system before the send, and the disk's confirmation is awaited by the first
  news that the order traded, never by the send. On a venue milliseconds away
  the barrier finishes during the flight, so that wait is nothing; `still
  waiting on the disk` is the new ledger segment that measures the residue.
  Measured with `engine bench --venue-delay-ms 4` — a new flag that holds the
  pretend venue at a real venue's distance, which a localhost socket cannot
  model — the same binary with one line changed goes from 9.59 ms to 6.01 ms
  p50 message-to-submit-result, and 13.69 ms to 6.31 ms p99. The tail moves
  further than the median because a slow barrier used to stack on top of the
  round trip and now hides inside it.

  What it gives up, stated rather than buried: a machine that dies inside the
  barrier can leave an order at the venue the log does not name, which
  reconciliation already reads as an order it cannot account for and answers by
  latching opening off. Process death is unaffected — those bytes are with the
  operating system either way. Nothing is acted on before its order is durable;
  what moved is when the path stops waiting, not what it waits for. The
  durability thread holds its own descriptor for the log and is replaced on
  rotation, since a barrier syncs the file rather than the path and a stale one
  would pass while proving nothing.

- **2026-08-29 — An accepted amend now keeps its order instead of cancelling
  it.** Bybit answers `order.amend` by saying it took the request and never by
  saying what price it left the order at, so every accepted reprice was
  cancelled rather than resolved to a price the engine could not name. The
  venue does state that price — it republishes the order on the private stream
  when it changes without trading — and the decoder was dropping the message as
  a repeat acknowledgement. It now becomes `OrderUpdate::Amended`, carrying the
  price and what is still working, and that is what narrows the conservative
  old/new reservation an amend opens. Hyperliquid's repeated `open` carries
  `limitPx` and does the same. An amend whose price is not stated within two
  seconds is cancelled, which is the behaviour every amend used to get. Three
  engine tests pin the three endings, each proved to fail with only its own
  mechanism removed.

- **2026-08-29 — The Bybit gateway paces to this account's real quota, and a
  declined order no longer costs the next one a reconnect.** Every trade-socket
  acknowledgement carries a `header` block stating the account's own per-second
  limit for the endpoint that was called; the adapter was dropping it and
  pacing forever to the documented default of ten. It now reads that figure and
  uses it when it is the larger, so a market-maker tier stops being invisible.
  A smaller figure is logged and not adopted: every batch is already capped at
  the documented default, so pacing below it would leave an admitted batch
  unable to reserve at all. Separately, the socket worker treated a business
  rejection like a broken pipe and tore the connection down, making the next
  order pay a reconnect and a re-authentication for a declined one. Only
  transport and decode failures drop it now.

- **2026-08-29 — The quoter takes its price from the top-of-book topic.**
  Bybit publishes depth-1 about twice as often as depth-50. The quoter
  subscribed only to the deep book, so the price it quoted around was up to one
  publication interval old. It now subscribes to both: the touch topic sets the
  microprice, and the book pressure, queue and variance terms stay on the deep
  book, which is the only thing that carries them. Subscribing to both exposed
  a latent fault in `MarketState::apply` — a depth event overwrote the quote
  slot unconditionally, so the deeper book's older copy of the touch replaced a
  fresher one. The touch is now arbitrated by socket read stamp, the only field
  comparable across two topics that each sequence themselves. With one stream
  the behaviour is unchanged, which is what the whole strategy suite passing
  untouched shows.

- **2026-08-29 — Cancel and amend timing marks reach the log.** The Bybit
  adapter captured exact socket-write and acknowledgement stamps for both, and
  the venue enum that the engine actually holds did not forward
  `take_mutation_timing`. It inherited the trait's `None`, so every cancel and
  amend wrote `null` and read back as "unknown" while placements were complete.
  Fixed, and the class closed: a source-reading test now requires the enum to
  write an arm for every method of `VenueGateway`, defaulted or not, with a
  negative control proving the scan is not blind. A method with a default body
  needs no arm to compile, which is what made this silent.

- **2026-08-29 — The order path separates the quota hold from the venue's own
  leg, and `engine latency` reads it back.** The 249.74 ms p99 venue task in
  the funded canary above was mostly the client's own rate pacing, which had to
  be inferred rather than read. Every place, cancel and amend now records how
  long the adapter held it back to stay inside the request quota, as its own
  mark in `VenueTiming`. The two ask for opposite fixes — a slow round trip is
  the network or the matching engine, a long hold is a quota to raise — and one
  span could not tell them apart. `engine latency --wal PATH` reports every
  step at p50, p90, p99 and p99.9 per operation from those exact stamps, rather
  than the live ledger's 60-second p50/p99 rollup. Checked against a real bench
  log: its per-step medians reproduce the bench's own ledger table, and the
  signing leg it splits out of the venue task measured 53.7 us.

- **2026-08-29 — The funded trade WebSocket completed a minimum-size forward
  trial.** The AGI canary's quoting run sent 256 placements, 237 amendments and
  258 cancels through the authenticated socket. Disabling it cancelled the one
  remaining quote and sent one market close through the same socket, leaving
  the account flat with no open order. Across the 256 quote placements,
  socket-write-to-ack measured 3.60 ms median, 20.41 ms p90 and 54.90 ms p99.
  The whole venue task measured 3.73 ms median; its 249.74 ms p99 includes the
  client's deliberate rate pacing before the socket write and is not network
  latency. The earlier signed-REST sample on the same host had only three
  placements, with a 45.62 ms median whole-task time and no socket-write mark,
  so the measured median task improvement is 12.2x while its tail is too small
  to compare honestly. Seventeen maker fills and the taker close completed
  eight round trips for -0.0779 USDT net after fees.

- **2026-08-29 — Funded Bybit order entry stays on the allowlisted IPv4.**
  The dual-stack resolver chose the VPS's Malaysian IPv6 address for
  `wss://stream.bybit.com/v5/trade`, whose CloudFront distribution rejected
  that country before authentication. The same official hostname reached a
  `101 Switching Protocols` response over `208.84.103.4` and authenticated the
  funded key with `retCode 0`, without sending an order. The persistent trade
  socket now resolves the official hostname but dials only IPv4, retaining TLS
  hostname verification, TCP no-delay and the signed REST fallback if a real
  WebSocket warm-up fails.

- **2026-08-29 — The minimum-size funded maker trial found and closed an
  inventory-ordering fault.** The AGI canary sent two orders and its first
  venue fill was a 750-unit maker buy at 0.006919, about 5.19 USDT. The next
  planned ask was larger than the position and still marked as an opening
  order, so the risk kernel correctly refused to let it cross through flat.
  A quote on the inventory-reducing side is now reduce-only, capped at the
  quantity held, and carries no replacement stop. An old opening quote on
  that side is cancelled to a terminal venue update before its replacement is
  sent. The registered mainnet canary stays in the append-only strategy table
  with `quote_enabled = false`, which pulls its orders and drains only its own
  inventory.

- **2026-08-29 — Bybit trade-WebSocket refusal no longer prevents account
  recovery.** The official `wss://stream.bybit.com/v5/trade` edge accepts the
  same handshake from the operator laptop but returns HTTP 403 before
  authentication to `208.84.103.4`; public REST and public/private WebSockets
  remain reachable from the host. The gateway still warms and authenticates
  the trade socket at every boot, but a failed warm now records the exact
  error and uses the already-warmed signed REST mutation path for that run.
  Private `execution.fast` remains independent.

- **2026-08-29 — Fast execution subscriptions are realm-specific.** Bybit
  demo refuses `execution.fast`, while mainnet exposes it. The first maker-path
  rollout therefore stopped at demo activation and its rollout transaction
  left every managed unit stopped; the funded engine never started and no
  order was sent. Demo now subscribes to `order` and fee-bearing `execution`;
  mainnet adds `execution.fast` for early strategy reaction.

- **2026-08-29 — The funded fleet moved to `208.84.103.4`.** The host passed
  strict SSH identity, exact two-IP key identity, signed account, public and
  private stream, target-book, commit, unit, and activation checks. The overdue
  ONT carry exit sold 790 at 0.05743 in four fills and left the account flat
  with no open order. Thirty warm signed position reads measured 12.71 ms
  median / 23.80 ms p95 on the fleet host and 172.14 ms / 486.59 ms on the
  declared `116.202.15.128` backup. The complete funded environment is staged
  on the fleet host; both addresses remain deliberately allowlisted.

- **2026-08-29 — An empty first book closes every position the log assigns to
  its sleeve.** A follower now seeds its candidate names from durable fill
  attribution as well as its config, current book, and in-process memory. An
  empty book therefore closes an owned non-seed position immediately after an
  engine restart, while positions attributed to another sleeve or no engine
  order remain untouched.

- **2026-08-29 — Bybit prices received before a subscription acknowledgement
  are preserved.** The public stream can send valid price frames before its
  acknowledgement. The feed now buffers those frames through the subscription
  phase and applies them in arrival order, after the reconnect boundary when
  there is one. Active strategy target books are also published group-readable
  (`0640`), so the isolated engine users can read decisions written by the
  producer; an unchanged book with an old private mode is republished.

- **2026-08-29 — A failed first Bybit market-data dial now waits before it
  retries.** The feed increased its backoff counter but slept only after a
  socket had connected once. An unavailable first socket therefore redialled
  in a tight loop, hit Bybit's WebSocket connection limit, and kept both
  engines blind. The first attempt remains immediate; every failure after it
  waits on the increasing capped backoff.

- **2026-08-29 — The funded key may declare one deliberate backup host.**
  `BYBIT_REAL_API_KEY_IP` remains the required primary address and the optional
  `BYBIT_REAL_API_KEY_BACKUP_IP` names one distinct backup. Startup compares
  the whole declared set with Bybit's signed key-identity reply, so an
  undeclared third address, a missing declared address, a duplicate, wildcard,
  or non-host network still refuses funded execution. Demo, producer,
  notification, and read-only attestation processes remove the backup setting
  from their environments.

- **2026-08-29 — The funded fleet can be resumed from the phone that paused
  it.** `resume-mainnet` joins the control helper's fixed action list and the
  sudo policy, which is now an exact five-command boundary. The funded resume
  proves this generation's completed activation receipt and that the funded
  account owner is running before it starts either producer, verifies both came
  up, and re-quarantines the pair if either did not. It never opens the
  credential file, so it cannot arm a disarmed account. Pausing real-money
  trading from a phone was previously a one-way door that needed a full rollout
  to undo.

- **2026-08-29 — Both liveness units can carry the dead-man's switch.** The
  watchdog already pinged `LIVENESS_HEARTBEAT_URL` on a healthy run, but no
  unit loaded a file that could carry it, so the switch could not be
  provisioned without editing a unit. Both units now read the optional
  root-owned `/etc/liquidity-migration/liveness.env`. Until that file names a
  URL the switch stays unprovisioned and a total host loss is still silent —
  which is what a rollout produces, because stopping the fleet stops the
  watchdog too.

- **2026-08-29 — `engine wal-cost` measures the storage's share of the order
  path.** The WAL crate already timed one buffered append against one
  durability barrier — the fsync a send waits for — but only a test could reach
  it. It is now a subcommand, so the cost can be read on the host that runs the
  fleet and again against a memory-backed path, which bounds what
  power-loss-protected storage would buy before any durability redesign is
  argued from guesswork.

- **2026-08-29 — The funded engine takes sole leverage authority.**
  The owner has stopped hand-trading the funded account, and the funded UID
  contract already forbids venue bots, copy trading, and other trading API
  keys. The funded engine therefore arms leverage when a target book arrives
  rather than inline before an order, and an entry from flat no longer pays a
  `set_leverage` round trip — measured live at ~172 ms, 844 ms worst, which
  was most of the order path's p99. Every held position's leverage is checked
  against the venue's own position row on each account reading; a contradiction
  alarms, is written to the log, and turns inline confirmation back on for that
  symbol, and a failed pre-arm is a warning rather than a refusal. A unit test
  requires both realms to state the value, because an absent key means
  `shared`.

- **2026-08-29 — A healthy funded watchdog no longer fails the rollout.**
  `start_mainnet_fleet` ended with `systemctl is-failed --quiet ... && fail`.
  A well funded liveness pass makes `is-failed` return non-zero, so that
  and-list — the function's last statement — returned 1, and activation aborted
  with no message at all. The guard now uses `if ... then fail`, as does the
  demo check that was correct only by its position in the caller. A unit test
  rejects any `&& fail` that ends a deploy function.

- **2026-08-28 — Both liveness observers get the same cgroup memory
  visibility as the producers.** `scripts/runtime/check_fleet_liveness.py`
  imports Polars and runs as the demo and funded liveness units, which still
  set `ProcSubset=pid`. Hiding the non-process `/proc` files kills that pass,
  and activation gates a rollout on the immediate demo pass succeeding, so the
  producer repair alone left the next rollout failing one phase later. The unit
  test now derives the Polars-reaching set from the committed dispatcher and
  the wrappers it names, rather than listing four producer unit names.

- **2026-08-28 — Preserved strategy-event tapes survive the engine wake
  cutover.** The deterministic tape reader retains the former
  `journal_change` spelling at the same data-arrival phase as `engine_change`.
  It verifies the original event IDs and rolling hashes without rewriting
  history, then permits current engine-wake records to append to that chain;
  unrelated event kinds remain rejected.

- **2026-08-28 — Rollout installs a runtime-usable Python generation and
  preserves producer state across the identity boundary.** Fresh virtual
  environments are root-owned mode `0755`, are import-smoked as every
  unprivileged Python runtime identity, and producer launchers no longer fall
  back to the host interpreter. Stopped installation migrates the demo and
  funded LONG/CARRY/Exodus state trees descriptor-relative, rehomes the two
  external LONG state files to their producer, and upgrades only the exact
  empty v1 LONG shape to v2 while preserving cooldowns. The LLM candidates
  handoff now lives inside the LLM service's own state directory; LONG receives
  group-read access without granting that service write access to engine target
  books.

- **2026-08-28 — The daily-loss circuit breaker is retired.** Operational
  profiles are schema v2 and no longer expose a daily-loss setting. The Rust
  engine neither restores nor writes its former control anchors, so legacy
  anchor state cannot block startup; historical anchor and verdict records
  remain readable, and WAL rotation drops them. Stopped installation also
  reassigns existing demo and funded engine-state trees in place to their
  isolated service identities, rejecting links, hard-linked files, and
  unsupported nodes instead of replacing durable state.

- **2026-08-28 — Isolated engines retain the existing account lease inode.**
  Stopped installation now gives persistent account-lease files root ownership
  and group write access for the isolated engine identities. The deployment
  preserves each file instead of replacing its flock inode, rejects links and
  non-regular paths, and lets both demo and funded services reopen leases made
  before the engines stopped running as root.

- **2026-08-28 — Bybit position-mode startup follows the row, not its cursor.**
  Explicit-symbol checks now request the venue's 200-row maximum and prove
  one-way mode from exactly one matching `linear` row with `positionIdx 0`.
  Demo and mainnet attach an opaque cursor to that complete response; following
  the observed demo cursor repeats the same row. Cursor presence no longer
  rejects a valid startup. Missing, duplicate, wrong-symbol, malformed, and
  hedge-mode rows still abort before a heartbeat or order.

- **2026-08-28 — Rollout recovery repairs producer inputs and lock cleanup.**
  Candidate-universe loading now accepts the deployment's exact immutable
  projection: root-owned mode `0640`, readable by the runtime group but not
  writable by producers. Private verifier-owned artifacts remain mode `0600`.
  This reconciles the producer loader with the installed demo and mainnet
  files without handing either producer authority to rewrite the reviewed
  universe. Lock-file orphan sweeping also invalidates its cache after a known
  staging mutation and bounds every clean cache entry, so equal or coarse
  directory mtimes cannot hide an abandoned alias indefinitely.

- **2026-08-28 — Rollout compilation leaves the incumbent fleet live.**
  The exact target commit now compiles during rollout prefetch. Stopped
  installation rechecks the immutable build source plus the candidate's path,
  owner, hard-link count, and prefetched SHA-256 before copying it, and performs
  no Cargo fetch or compilation. Prefetch fills a clean locked Cargo cache,
  then runs proc macros and build scripts offline in a private network. This
  phase also fetches and binds the target branch and downloads the exact-version
  Python wheels into a byte-digested cache; stopped install builds a fresh
  environment only from that cache with `--no-index`, proves its distribution
  set exactly matches the lock, and atomically exchanges it with the prior
  environment. Transient builders have a runtime bound and are stopped on exit
  or signal. A cancellation before the stop boundary leaves the incumbent
  topology untouched. This removes dependency downloads and the release build
  from the service outage without changing the installed artifact bindings.
  Each prefetch also scrubs its disposable compiler checkout before verifying
  the exact commit, so stale benchmark output and cross-platform metadata cannot
  block or contaminate a later rollout. Cargo's ordinary hard-linked promoted
  binary is confined to the disposable target, byte-verified into an atomic
  single-link handoff, and only that handoff can reach stopped installation.
  Fresh Python dependency verification now enumerates only that generation's
  own site-packages, so stale source-tree metadata cannot enter or reject the
  exact installed-distribution comparison. Telegram control-policy comparison
  also canonicalizes each command before sorting, making its exact four-command
  proof independent of sudo's presentation order. Deployment now also
  reconciles legacy demo-engine environments with the committed exact account,
  venue, and realm binding before the build. Missing bindings are appended
  atomically while host-only dials are preserved; empty or conflicting bindings
  abort without modifying the file. The installed release directory is now
  root:root mode `0755`, matching the activation watchdog's trust boundary, and
  release verification checks that parent before permit creation.

- **2026-08-28 — Exodus handoff uses the position actually abandoned.**
  A v7 pre-settlement fire now snapshots the fresh carry-attributed venue
  quantity and the same ticker's mark price. Target-book v2 carries that exact
  signed quantity alongside its frozen audit notional and direct entry
  deadline; the Rust follower converges entry and partial fills by quantity,
  so later price movement cannot resize the handoff. Legacy v1 target books
  and Exodus state remain readable, while new state is schema v2. The obsolete
  `EXODUS_NOTIONAL_MULTIPLIER` dial is removed. Heartbeat working-entry rows now
  come from a counted live-order index rather than scanning all orders retained
  in the current WAL segment, keeping account-state publication cost bounded by
  live work as history grows.

- **2026-08-28 — Funded risk configuration has one runtime source.**
  The engine now reads the same preflight-validated operational-profile artifact
  as the funded producers. Carry's rendered stop declaration can widen the
  engine baseline but cannot narrow the ceiling used by LONG and Exodus. This
  removes the case where a valid operator dial passed producer preflight and
  was then refused by an engine still holding the committed default.

- **2026-08-28 — Exodus short joins the funded engine as sleeve three.**
  The funded carry producer selects `lane2_exodus_short_v1` and writes
  `exodus-mainnet.json`; the funded engine consumes it as the appended
  `exodus` strategy, crosses entries and covers, and reports its book and fills
  separately. Carry and long keep WAL ids zero and one; boot accepts this
  suffix addition but still refuses any reorder. Once that longer Names record
  reaches the WAL, recovery requires a three-sleeve-compatible binary and
  config. Deployment installs the committed funded engine config atomically,
  validates, quarantines, waits for, flattens, and notifies the funded Exodus
  book. No
  synthetic venue order is used: the first live order waits for a real v7
  pre-settlement exit fire.

- **2026-08-28 — Rollout no longer depends on account-flatness attestation.**
  The deployment path no longer snapshots an outgoing attestor, runs account
  inventory at three rollout phases, accepts `--require-flat`, or requires a
  mainnet attestor credential during activation. It also stops asking the
  outgoing generation for a release marker and activation receipt before the
  fleet is stopped, so the markerless `e4e6750` production generation can cross
  the upgrade boundary. The target build, release binding, ordered fleet stop,
  persistent boot fence, quiescence check, activation lease, target-topology
  verification, and rollback/quarantine handling remain. `attest-flat` stays
  available as an explicit read-only operator command and for loss reset. The
  arbitrary 2026-08-27 key-creation cutoff is also gone; funded identity still
  requires UTA, write access, exact single-host IP, ContractTrade Order and
  Position permissions, no withdrawal permission, and the dedicated account ID.
  A pre-install failure now restores the exact active and persistent/runtime
  enablement topology it observed. Markerless incumbents restart directly;
  marked releases receive a temporary binding to the unchanged artifacts while
  only observed units restart, followed by a replacement completion receipt. A
  failure after checkout mutation leaves the fleet stopped.

- **2026-08-28 — Opening-stop lookup stays flat as order history grows.**
  The live-order ledger maintains a per-symbol, per-side multiset of opening
  stop prices and exposes only each side's tightest level to placement. This
  replaces a full allocation and scan of every outstanding order before every
  batch without moving the durability boundary or weakening whole-position
  stop protection. On the production host's memory-backed filesystem, the
  10,000-order durability median fell from 265 µs to 14.6 µs; the real-WAL
  5,000-order run fell from about 29 s to 7.83 s. Three standard 1,000-order
  native runs put the local submit-result median at 1.26 ms and the
  median-of-runs p99 at 3.16 ms. The Bybit aggregate-inventory tests also state
  their fixtures' actual row counts, restoring the Ubuntu release gate without
  changing production parsing. Private-stream integration tests consume the
  first successful subscription's readiness reset and prove the same reset
  precedes updates after reconnect, matching the runtime contract.

- **2026-08-28 — Venue-mutation bursts yield at bounded safety boundaries.**
  One strategy wake retains FIFO order, its original latency clock, and its
  flood limits across cooperative turns. After each completed placement,
  cancel, amend, or stop mutation, the engine gives ready private lifecycle
  updates and a due account refresh priority before sending the next group;
  an already-selected trailing exit still completes when shutdown becomes
  ready. The strategy-host heartbeat watcher now completes an installation
  handshake and compares the decision projection across both inotify and
  polling handoffs, closing the immediate-start rename gap. Release CI runs
  the optimized engine suite, bounded account-history soak, order-path
  benchmark, and artifact smoke test. Funded disarm remains available when CI
  is red, preempts a running rollout, shares one bounded lock deadline, and a
  canceled rollout leaves the fleet stopped. Rollout builds require the pinned
  Rust toolchain during prefetch as well as compilation. Latency output and
  standing docs call the measured local boundary a parsed submit result; the
  available records do not establish a socket-write timestamp.

- **2026-08-28 — Audit series pushed; Ubuntu qualification is billing-blocked.**
  The 42-commit Rust-only migration series, ending in audit commit `206e40c21`,
  was fast-forwarded to `main`. Push workflow run `33130163698` created both
  Ubuntu jobs, but GitHub rejected each before assigning a runner or executing
  a step because recent account payments failed or the Actions spending limit
  must be increased. This is not a passing or failing test result: release
  qualification remains pending until the account owner fixes billing and the
  exact pushed commit's Python, Rust, bounded soak, build, and smoke steps run
  green. No VPS deploy or live venue order was performed.

- **2026-08-27 — The seven execution-audit gaps become explicit Rust and
  rollout contracts.** Sibling placements now validate and reserve in request
  order, append together, cross one WAL barrier, and reach Bybit as overlapping
  distinct-symbol HTTP chains over a ten-socket warm pool; same-symbol and
  nonce-sensitive chains retain serial wire order. Each mutation endpoint has
  a completion-anchored rolling quota, and native batch cancellation pulls a
  halted book in bounded ten-order groups while private terminal updates stay
  ahead of confirmation deadlines. Risk reservations include cumulative
  opposite-side pending quantity and restart charges only each order's
  unfilled remainder. Opening reprices require finite risk approval and retain
  their full old/requested price range through ambiguity, rotation, and
  restart, so high-price notional and low-price short-stop loss are both
  charged until a definitive answer or cancel. Whole-position stop intent now belongs to the fill that
  actually grows or crosses the position, never an unfilled sibling;
  same-side growth cannot loosen the tighter existing level, pre-wire checks
  include prior-wake live orders, and fresh account views actively repair any
  venue regression or latch opening off. Malformed daily-loss anchors abort
  startup instead of silently resetting the circuit breaker. Before fetch,
  rollout digest-verifies and freezes the outgoing installed engine. That
  immutable binary performs the pre-stop and owners-stopped flatness checks;
  the final boundary requires both it and the digest-bound installed target,
  while the incoming checkout and build candidate never attest. An outgoing
  release without `attest-flat` requires a signed, reviewed out-of-band
  bootstrap rather than falling back to incoming code. Mainnet checks
  receive only a separate globally read-only query key from an exact-schema,
  operator-owned attestor file, never the execution key. Direct install,
  activate, staged, and funded unit start/restart paths no longer bypass
  rollout on a funded-configured host. Mainnet inventory covers ordinary,
  spread, RFQ, active venue-native strategy, and reported cross-account
  asset/bot state. Nonadditive venue aggregates are not treated as an API
  guarantee, while aggregate-only values cannot masquerade as cash unless
  coin detail explicitly identifies positive USDT/USDC. Because Bybit cannot enumerate every bot instance,
  funded identity also requires an account-bound acknowledgement that its UID
  is dedicated to the engine with no hand trading, venue bots, copy trading, or
  other trading API keys. Rollout activation now uses a root watchdog to renew
  a boot- and process-bound ten-field six-second permit while trusted launchers
  supervise the candidate topology; only a synced, verified six-field release
  receipt survives reboot, so process death or power loss cannot preserve a
  partial activation. Permit renewal now records the pre-validation inode,
  takes a non-creating pin, and revalidates it under lock, so direct deletion or
  a valid-looking replacement cannot race recreation or adoption. Remote
  funded stop/disarm execute no checkout code; stop never reads credentials,
  and disarm uses an isolated root-owned interpreter with an embedded strict
  atomic rewrite after persistent quarantine. Deploy preflight and launchers reject
  writable critical checkout ancestry or Git metadata. Bybit startup verifies one-way mode
  for every configured or newly admitted symbol. Execution recovery aborts
  instead of clipping intervals older than venue history. Ubuntu CI runs the
  bounded-ID release soak that separates within-run ID cost from synthetic
  recovery-history cost. UTC loss rollover now carries bounded durable pre-midnight equity
  evidence (periodically and immediately on rises), preventing a crash or the
  first post-midnight order from erasing a boundary loss without making every
  account poll an unconditional fsync. Hyperliquid and Lighter testnets remain canary paths, while their
  mainnets and MEXC mainnet are source-gated from `engine run` until exact-realm
  live lifecycle evidence exists; public-feed continuity checks now match each
  protocol's evidence. Funded risk gains a durable 10 USDT UTC-day account-loss
  halt plus a stopped-engine, flat-account `loss-reset`; demo leaves it
  disabled. Standing docs now match fail-closed foreign-activity handling,
  direct adapter rule reads, and the remaining live-validation boundaries.
  Funded Bybit identity now rejects the exposed key generation and unsafe key
  shapes: keys must be created on or after 2026-08-27 22:30 UTC, UTA,
  write-capable, allowlisted only to the declared production host IP,
  ContractTrade Order+Position capable, and unable to withdraw. Creating the
  replacement and revoking the old key remain external owner actions.

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
