# VPS systemd deployment

The active VPS service is `model050426-bybit-demo.service`.

Install or refresh it on the VPS:

```bash
cp deploy/systemd/model050426-bybit-demo.service /etc/systemd/system/model050426-bybit-demo.service
systemctl daemon-reload
systemctl enable --now model050426-bybit-demo.service
systemctl restart model050426-bybit-demo.service
```

Required secrets live outside git in:

```text
/etc/model050426/bybit-demo.env
```

That environment file must define the Bybit demo API credentials and Telegram
credentials. The service submits demo orders only and uses the promoted
`volume-events` defaults from the repo.

The retired `model050426-bybit-demo-signal.timer` / `.service` daily signal scan
must stay disabled; the active runner is the event-driven loop above.
