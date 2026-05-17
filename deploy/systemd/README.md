# VPS systemd deployment

The active VPS services are:

- `model050426-bybit-demo.service`: event entry/normal lifecycle runner.
- `model050426-bybit-risk.service`: fast exit-only risk runner.
- `model050426-bybit-canary.timer`: optional 30-minute demo order-path canary.

Install or refresh it on the VPS:

```bash
cp deploy/systemd/model050426-bybit-demo.service /etc/systemd/system/model050426-bybit-demo.service
cp deploy/systemd/model050426-bybit-risk.service /etc/systemd/system/model050426-bybit-risk.service
cp deploy/systemd/model050426-bybit-canary.service /etc/systemd/system/model050426-bybit-canary.service
cp deploy/systemd/model050426-bybit-canary.timer /etc/systemd/system/model050426-bybit-canary.timer
systemctl daemon-reload
systemctl enable --now model050426-bybit-demo.service
systemctl enable --now model050426-bybit-risk.service
systemctl enable --now model050426-bybit-canary.timer
systemctl restart model050426-bybit-demo.service
systemctl restart model050426-bybit-risk.service
systemctl start model050426-bybit-canary.service
```

Required secrets live outside git in:

```text
/etc/model050426/bybit-demo.env
```

That environment file must define the Bybit demo API credentials and Telegram
credentials. Telegram is enabled for material alerts only: entries, exits,
position reconciliation, or position-report errors. Quiet no-trade cycles still
write local reports but must not notify. The services submit demo orders only
and use the promoted `volume-events` defaults from the repo. The risk service
does not open entries; it repairs exchange-native stop/TP state, listens to
demo private WebSocket position/order/execution streams plus the mainnet public
ticker stream, and submits reduce-only exits. On the demo account, WebSocket
decides exits while REST remains the order-submit fallback because Bybit
WebSocket Trade does not currently support demo trading. The demo socket uses
the normal private execution stream; `execution.fast` is disabled because the
demo private socket rejects that topic.
`STREAM_START_TIMEOUT_SECONDS` bounds private/public WebSocket startup so a
blocked subscription is reported while REST reconciliation and exchange-native
stops keep covering open risk.

The canary timer exists because the promoted alpha is sparse. It places and
immediately cancels a far-from-touch post-only demo order, verifies cleanup, and
writes reports under `reports/demo-canary`. Canary reports are operational
evidence only; they are not strategy trades and do not write the strategy trade
or order ledger.

The retired `model050426-bybit-demo-signal.timer` / `.service` daily signal scan
must stay disabled; the active runner is the event-driven loop above.
