# Retire an obsolete Lightsail data disk

Use `scripts/retire_lightsail_disk.sh` from an authenticated AWS CloudShell with
Bash, Python 3, AWS CLI v2 and curl. Upload the reviewed script through CloudShell's
file upload control; it is standalone and needs no repository checkout or
credentials copied from the server. The script supports this specific migration:
an old **256 GiB** additional disk and a retained **512 GiB** additional disk.
Deployment names, UUID and HTTPS origin are runtime arguments.

This maintenance stops the instance and temporarily interrupts the API. AWS
requires unmounting the obsolete filesystem before stopping and detaching its
disk; deletion requires the disk to be `available`. See the official
[detach command](https://docs.aws.amazon.com/cli/latest/reference/lightsail/detach-disk.html)
and [delete command](https://docs.aws.amazon.com/cli/latest/reference/lightsail/delete-disk.html).

Before stage one, independently verify on the server that:

- The replacement filesystem UUID is mounted at the data directory and is the
  sole corresponding persistent `/etc/fstab` entry. The old disk is unmounted,
  has no swap or other consumers, and is not referenced by a boot dependency.
- The updater has stopped gracefully, including its database shutdown, with no
  running worker or pending start job. Its automatic starts remain blocked for
  maintenance. Keep the serving API healthy until the controlled instance stop.
- All data needed from the old disk has complete recovery archives elsewhere,
  with full checksums verified against the source. A checksum alone is not a
  restore test; review recovery usability. Separately maintain private backups
  of configuration and credentials needed for a full deployment recovery.
- The API's exact HTTPS origin passes its certificate and health checks.

Do not acknowledge maintenance readiness while a database is still shutting
down. The script cannot inspect server mounts, processes or backup contents.

Create a private receipt directory in CloudShell and fill in your reviewed
deployment values. Keep this shell and the receipt between stages:

```bash
OJS_RETIRE_DIR=$(mktemp -d "$HOME/disk-retirement.XXXXXXXX")
OJS_RETIRE_RECEIPT="$OJS_RETIRE_DIR/receipt.json"

bash ./retire_lightsail_disk.sh detach \
  --region YOUR_AWS_REGION \
  --instance YOUR_INSTANCE_NAME \
  --old-disk YOUR_256_GIB_DISK_NAME \
  --keep-disk YOUR_512_GIB_DISK_NAME \
  --keep-uuid YOUR_RETAINED_FILESYSTEM_UUID \
  --api-origin https://YOUR_API_HOSTNAME \
  --receipt "$OJS_RETIRE_RECEIPT" \
  --maintenance-ready
```

The first stage identifies the AWS account, instance and both disks by their
exact names and ARNs, region, availability zone, sizes and attachments. It saves
a mode-0600 receipt in your mode-0700 directory before changing anything, stops
the instance normally, waits for `stopped`, detaches only the old disk, waits for
`available`, starts the instance and waits for HTTPS health. It checks retained
disk identity and attachment throughout. TLS verification stays enabled, curl
configuration and proxies are ignored, and redirects are refused. The only
application request is `/health` at the exact supplied origin; AWS API requests
use the authenticated AWS CLI.

If this stage fails or is interrupted, **do not rerun it with a new receipt**.
Inspect the saved stage, last operation identifier and AWS resources with an
operator. An operation can finish after a client timeout. The script does not
guess recovery actions, restart a stopped instance on an error path, overwrite
an existing receipt, or automatically retry mutations. The old disk remains
preserved unless you separately invoke stage two.

After stage one succeeds, independently connect to the restarted instance over
SSH. Confirm the new boot and intended instance identity, the retained UUID and
512 GiB filesystem at the persistent data mount, the old filesystem's absence,
and the API/database bind mounts. Verify authenticated catalogue endpoints,
snapshot dates and cumulative history against your expected pre-maintenance
values. Use the existing [API history checker](../scripts/check_api_history.py)
and your private credentials without copying them into CloudShell or chat.
Public `/health` alone cannot establish that the correct published history is
mounted. Keep automatic updates blocked until these checks finish.

Only after those independent checks succeed, run the separate irreversible
deletion step in the original CloudShell:

```bash
bash ./retire_lightsail_disk.sh delete \
  --receipt "$OJS_RETIRE_RECEIPT" \
  --verified-detached-boot-mount-api-history
```

Stage two requires the successful stage-one receipt and the explicit independent
verification acknowledgement. It rechecks the AWS account, all three ARNs,
region, sizes, running instance, retained-disk attachment and old disk's
`available` state; it requires fresh trusted HTTPS health and rechecks resource
identities immediately before deleting. It waits for AWS operation success and
the old disk's absence from the complete disk inventory, rechecking the retained
disk and running instance, before recording completion. An API, TLS, identity,
attachment or AWS error stops the
workflow. The receipt is a private operator record, not a cryptographically
signed attestation of the independent server checks; do not edit it.

The script never formats filesystems, changes the retained disk, creates paid
snapshots/resources, forces an instance stop, force-detaches a disk or deletes
disk add-ons. Deleting the old disk cannot be undone through the receipt; recover
its contents from your independently verified external backups if needed.
After completion, inspect the console's disk inventory and billing, then restore
the previously approved update schedule only after normal service is verified.
