# Execution host storage and recovery

## Purpose

Define the storage separation and standby cutover required to remove the funded engine's single host and single disk dependency.

## Spec Tables

| Component | Current verified state | Required destination |
|---|---|---|
| Funded and demo WAL | Root ext4 on one 119 GiB QEMU disk | Dedicated execution volume; no tape or backup staging writer |
| Bybit tape | `/var/lib/liquidity-migration/forward-market` | Independent data volume, mounted at the existing path |
| Binance tape | `/var/lib/liquidity-migration/forward-market-binance` | Independent data volume, mounted at the existing path |
| Backup stage | `/var/lib/liquidity-migration/backup/stage` | Independent data volume; keep rclone configuration outside the tape directories |
| Second host | Absent | Passive recovery host in a separate failure domain; deployment binaries and a verified restored WAL family |
| Provisioning | No second writable disk is attached | Provider, monthly budget and new-volume identity require owner input |
| Capacity | A quoted 6.5 MB/s sustained write rate is 561.6 GB/day before compression | Size from retained compressed bytes and measured peak backlog; include two interrupted upload windows plus full staging size |
| Backup cadence | Installed from `liquidity-migration-backup.timer` | 15-minute starts; 10-minute run budget; alert when the last completed copy is over 30 minutes old |
| Recovery point | Last completed remote copy, not timer activation | Scheduled copies can still lose the interval plus transfer time; they are not synchronous WAL replication |
| Clock | NTP state plus public venue-time sampling in host liveness | Alert above 250 ms offset after subtracting half RTT; requests over 1 s are inconclusive, not a drift measurement |

| Cutover step | Operation | Acceptance |
|---|---|---|
| Prepare | Attach and format an explicitly selected new data volume; mount it at `/mnt/liquidity-data` | `findmnt` shows a different device from both WAL roots |
| Copy | Initial `rsync -aHAX --numeric-ids` of both tape roots and the stage while writers remain active | Destination fits with headroom; an initial copy is not a consistent cutover |
| Quiesce | Stop the two capture services, tape upload timer/service and backup timer/service; keep engines and signal workers running | No open writer remains on any source being moved |
| Final copy | Repeat rsync, then compare with `rsync -nrc --delete` | No content or path difference |
| Mount | Keep the old directories as rollback copies; bind mount the copied directories at their canonical paths; persist UUID and bind mounts in `/etc/fstab` | `findmnt -T` shows data volume for tape/stage and execution device for WAL |
| Depend | Add `RequiresMountsFor=` for the canonical tape/stage paths to capture, upload and backup units | A missing data mount fails those units instead of writing onto the WAL filesystem |
| Resume | Reload systemd; start the stopped services and timers | Fresh tape frames, successful upload and complete remote backup; engine WAL continues on its original device |
| Reclaim | Verify contents, open-file paths and off-box copies before removing redundant rollback directories | Never delete the sole research copy, WAL family or quarantine evidence |
| Standby restore | Restore a complete WAL family and configuration on the passive host; verify release hashes and replay | No trading credentials or active gateway on the passive host |
| Funded takeover | Stop and fence the old account owner, restore its final durable state, reconcile authenticated executions, then activate one new owner through sanctioned deployment | Local file leases do not fence a different host; no automatic promotion without an independent fence |

## Invariants

- Must preserve canonical WAL and tape paths so backup and recorder consumers agree after migration.
- Must keep recorder and backup I/O off the execution volume; another directory on the same filesystem does not satisfy this requirement.
- Must preserve full retained WAL families and unknown-outcome identities across restore.
- Must never run two funded owners or copy venue credentials into tape or cloud backup payloads.
- Must report the latest restored execution and tested recovery time; a passive server alone is not proven failover.

## Operational Recipes

Run these read-only inventory commands before selecting a volume or cutover:

```sh
ssh root@208.84.103.4 'lsblk -o NAME,SIZE,FSTYPE,UUID,MOUNTPOINTS'
ssh root@208.84.103.4 'findmnt -T /var/lib/liquidity-migration-engine-mainnet; findmnt -T /var/lib/liquidity-migration/forward-market; findmnt -T /var/lib/liquidity-migration/backup/stage'
ssh root@208.84.103.4 'du -sx --block-size=1 /var/lib/liquidity-migration/forward-market /var/lib/liquidity-migration/forward-market-binance /var/lib/liquidity-migration/backup/stage'
ssh root@208.84.103.4 'systemctl list-timers --all liquidity-migration-backup.timer; cat /var/lib/liquidity-migration/receipts/backup.last-success'
```

The cutover remains unexecuted until a separate volume and passive host are available. The existing root disk is not a migration target.
