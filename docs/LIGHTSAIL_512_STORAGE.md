# Plan a Lightsail data disk migration

This is a storage-review checklist, not a command to buy or format a disk.
Keep operator IP addresses, disk UUIDs, serial numbers and AWS account IDs in
private notes outside Git.

## Capacity comes first

Measure database **apparent** and allocated sizes: source filesystem compression
or sparse allocation may not carry over. Add the latest clean export, incoming
transfer, rollback copy and reserve. A serving release fitting a 512 GiB disk
does not mean a new wrangle fits alongside it: raw tables, candidate tables,
indexes and scratch files can temporarily coexist.

Do not lower reserves to force a build through. Prefer a separate builder when
the measured peak exceeds serving capacity. Review provider storage, snapshot
and transfer pricing before provisioning; a detached old disk can still incur
charges.

## Identify disks without modifying them

Run on the VM:

```bash
sudo udevadm settle
lsblk -b -o NAME,PATH,SIZE,TYPE,FSTYPE,LABEL,UUID,SERIAL,MOUNTPOINTS
findmnt /srv/ojs_api/data
df -h / /srv/ojs_api/data
sudo systemctl show ojs-api-update.service -p ActiveState -p SubState -p MainPID
sudo ps -C mysqld -o pid,ppid,stat,etime,comm
```

Treat this output as private. Check the exact volume in the AWS console
independently. Never infer a target from device name alone or format the OS
disk, a mounted disk or one with unreviewed signatures. A formatting procedure
must validate size, serial, signatures, holders and mounts before any write.

## Replacement choices

For production data, stop writes cleanly and choose a verified snapshot/restore
or cold-copy procedure, preserving rollback. For a new deployment with an
authoritative completed release elsewhere, an empty new filesystem can receive
that release directly. The switching helper **does not copy old disk contents**.

`scripts/switch_lightsail_data_disk.sh` is deliberately restricted to an already
formatted 512 GiB ext4 disk labelled `ojs-data-512`. It requires an offline
`/srv/ojs_api` deployment, no Docker containers, a saved cron entry at
`/etc/ojs-api-cron.paused`, no updater trigger, and explicit old/new identifiers.
It backs up fstab, switches the mount, leaves the old disk intact and blocks the
updater. It neither formats a disk nor starts a database. Usage:

```text
sudo bash scripts/switch_lightsail_data_disk.sh DEVICE NEW_UUID NEW_SERIAL OLD_UUID FSTAB_BACKUP
```

The backup must be a previously unused `/etc/fstab.ojs-before-512-YYYYMMDD` path.
Read the complete helper and verify prerequisites before use. It refuses layouts
outside its narrow contract; do not bypass checks for a different migration.

## Verify before publication or cleanup

Confirm the expected UUID at `/srv/ojs_api/data`, persistent fstab configuration,
permissions and Docker mount dependency. Run the [deployment preflight](../LIGHTSAIL_DEPLOYMENT.md),
then the [release handoff](API_HISTORY_HANDOFF.md). Keep scheduling paused.

Only after authenticated API/history checks and recovery validation should an
operator decide what old artifacts can be removed. Confirm exact disk identity,
absence of mounts/users and a verified replacement before deleting any volume.
No deletion, formatting or AWS purchase is authorized by this guide alone.

Reference: [Lightsail block storage FAQ](https://docs.aws.amazon.com/lightsail/latest/userguide/amazon-lightsail-faq-block-storage.html).
