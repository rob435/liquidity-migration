# VPS systemd deployment

The active VPS services are:

- `model050426-bybit-demo.service`: event entry/normal lifecycle runner.
- `model050426-bybit-risk.service`: fast exit-only risk runner.

Install or refresh it on the VPS:

```bash
cp deploy/systemd/model050426-bybit-demo.service /etc/systemd/system/model050426-bybit-demo.service
cp deploy/systemd/model050426-bybit-risk.service /etc/systemd/system/model050426-bybit-risk.service
systemctl daemon-reload
systemctl enable --now model050426-bybit-demo.service
systemctl enable --now model050426-bybit-risk.service
systemctl restart model050426-bybit-demo.service
systemctl restart model050426-bybit-risk.service
```

Required secrets live outside git in:

```text
/etc/model050426/bybit-demo.env
```

That environment file must define the Bybit demo API credentials and Telegram
credentials. Telegram is enabled for material alerts only: entries, exits,
position reconciliation, or position-report errors. Quiet no-trade cycles still
write local reports but must not notify. The services submit demo orders only.
The entry service currently uses `STRATEGY_PROFILE=demo_relaxed`, a higher-frequency
test-only profile with separate full-PIT evidence in `docs/system_status.md`.
It shares the promoted strategy's conservative `promoted_quality_squeeze` entry
router for promoted-grade events but keeps relaxed `demo_relaxed` gates for
forward plumbing visibility. It is not the promoted research default. The risk
service does not open entries; it repairs exchange-native stop/TP state, listens to
demo private WebSocket position/order/execution streams plus the mainnet public
ticker stream, and submits reduce-only exits. On the demo account, WebSocket
decides exits while REST remains the order-submit fallback because Bybit
WebSocket Trade does not currently support demo trading. The demo socket uses
the normal private execution stream; `execution.fast` is disabled because the
demo private socket rejects that topic.
`STREAM_START_TIMEOUT_SECONDS` bounds private/public WebSocket startup so a
blocked subscription is reported while REST reconciliation and exchange-native
stops keep covering open risk.

The retired `model050426-bybit-demo-signal.timer` / `.service` daily signal scan
must stay disabled; the active runner is the event-driven loop above.
