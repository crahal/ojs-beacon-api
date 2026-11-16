# Deploy on AWS Lightsail

This guide installs a single-host API on Ubuntu 24.04 LTS, x86-64, using a
dedicated `ojs` account and `/srv/ojs_api`. **VM** commands run in the Ubuntu SSH
terminal; **desktop** commands run on the machine holding a completed catalogue.
Provisioning in **AWS CloudShell/console** is separate. Replace all `YOUR_...`
placeholders. This is a fresh-install guide: preserve existing configuration
when upgrading. `203.0.113.10` and `api.example.org` are documentation examples,
not service addresses.

Data and credentials are not included. Confirm permission to access and
redistribute the source data independently of the repository's code licence.

## 1. Capacity and deployment decision

The default profile targets 4 GiB RAM with one metadata worker, compressed
tables/input and emergency swap. The builder is capped at 2 GiB, serving MySQL
at 1 GiB and the API at 256 MiB. Preflight validates configuration and headroom,
not full-data capacity. Processing can take days.

Prefer **build elsewhere, serve here** if the VM's build peak is unknown. One
completed multi-snapshot catalogue measured about 186 GiB of apparent database
files plus a 41.5 GiB export. Budget apparent bytes, not compressed filesystem
allocation. A 256 GB disk leaves insufficient safe room for that package. A
512 GB disk can hold it, but does **not** establish that another full build
fits alongside it.

An update can require the old live database, latest clean export, new raw gzip,
candidate raw/clean tables, indexes, SQL temporary space, new export and safety
reserve simultaneously. The 20 GiB starting floor and 8 GiB reserve are stop
guards, not capacity predictions. Never lower them to force a full-disk build.

Review current [AWS prices](https://aws.amazon.com/lightsail/pricing/) before
provisioning. Instances, attached/retained disks, snapshots and excess transfer
can each incur charges. Detaching a disk does not stop its storage charge.
Keep backups bounded, with explicit retention and tested recovery.

## 2. Provision and mount storage

**AWS console/CloudShell:** create an Ubuntu instance and attach a static IPv4
address. For a hostname, point DNS to that address. Allow TCP 22 only from current
administrator IPs; retain Lightsail browser-SSH access if needed. Allow TCP 80
and 443 publicly. Do not expose 3306 or 8000. Review IPv6 rules too, or leave it
unused. [AWS firewall documentation](https://docs.aws.amazon.com/lightsail/latest/userguide/understanding-firewall-and-port-mappings-in-amazon-lightsail.html).

**VM:** install prerequisites and clone a reviewed deployment revision:

```bash
sudo apt update
sudo apt install -y ca-certificates curl git gzip make openssl python3 rsync util-linux cron
sudo timedatectl set-timezone UTC
sudo useradd --create-home --shell /bin/bash ojs
sudo install -d -o ojs -g ojs /srv/ojs_api
sudo -u ojs -H git clone https://github.com/crahal/ojs-beacon-api.git /srv/ojs_api
sudo -u ojs git -C /srv/ojs_api log -1 --oneline
```

Review/install OS security updates before workloads start. Do not reboot during
a build, transfer or publication. If the user/checkout exists, use the upgrade
procedure rather than cloning over it.

Use the canonical source above for a new deployment. Do not repoint an active
operational checkout to it or merge unrelated private history as part of setup.

Attach a data disk in the same Availability Zone. Match the AWS volume identity,
full Linux serial and exact byte size before formatting:

```bash
sudo udevadm settle
lsblk -b -o NAME,PATH,SIZE,TYPE,FSTYPE,LABEL,UUID,SERIAL,MOUNTPOINTS
```

**Formatting destroys the selected disk.** Only a verified new blank device may
be formatted; never guess its Linux name. Follow the
[AWS disk installation procedure](https://docs.aws.amazon.com/lightsail/latest/userguide/create-and-attach-additional-block-storage-disks-linux-unix.html)
for that exact device. Moving an existing data volume needs a separately
reviewed migration, retaining the old disk until verification.

After formatting the verified blank disk as ext4, create its mountpoint:

```bash
sudo install -d -o ojs -g ojs /srv/ojs_api/data
sudo editor /etc/fstab
```

Add one entry using its actual filesystem UUID; preserve root, boot and swap:

```fstab
UUID=YOUR_DATA_FILESYSTEM_UUID /srv/ojs_api/data ext4 defaults,noatime 0 2
```

```bash
sudo systemctl daemon-reload
sudo findmnt --verify
sudo mount /srv/ojs_api/data
findmnt --mountpoint /srv/ojs_api/data
df -h /srv/ojs_api/data
sudo chown ojs:ojs /srv/ojs_api/data
sudo -u ojs install -d /srv/ojs_api/data/raw /srv/ojs_api/data/clean
sudo -u ojs touch /srv/ojs_api/data/.updates-paused
```

Verify the mount uses the intended data disk, not root. The transfer helpers
require this dedicated filesystem. The pause marker prevents future automatic
work; it does not stop an existing process.

## 3. Install runtime dependencies

**VM:** install Docker Engine and Compose using the
[official Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/).
Then grant the service account access:

```bash
sudo usermod -aG docker ojs
sudo systemctl enable --now docker
sudo -u ojs -H docker version
sudo -u ojs -H docker compose version
```

Docker group membership is root-equivalent. Keep the account dedicated. Retain
the loopback-only API binding and unexposed MySQL port even with a host firewall.

Run `sudo systemctl edit docker.service` and enter this above the editor's
discard line:

```ini
[Unit]
RequiresMountsFor=/srv/ojs_api/data
```

```bash
sudo systemctl daemon-reload
sudo systemctl restart docker
systemctl show docker.service -p RequiresMountsFor
```

Install **MySQL Community 8.4 LTS** using the
[official APT guide](https://dev.mysql.com/doc/mysql-apt-repo-quick-guide/en/).
The host needs all four binaries below; Ubuntu's default major series may differ.
Disable the unused distribution-managed server:

```bash
sudo systemctl disable --now mysql
mysqld --version
mysql --version
mysqladmin --version
mysqldump --version
```

Host MySQL, the serving image and an imported physical candidate must use the
**same exact patch**. The candidate's `OJS_COMPACT_RELEASE.json` records
`mysql_version`. Set `OJS_MYSQL_IMAGE` accordingly; never use `latest` or assume
the sample tag fits. Plan host/image upgrades together in a stopped, backed-up
maintenance window. Prevent unattended patch drift without indefinitely
deferring security updates.

If MySQL has an AppArmor profile, merge `deploy/apparmor-mysqld-ojs` into its
local include, preserving unrelated rules, and reload the actual profile:

```bash
sudo editor /etc/apparmor.d/local/usr.sbin.mysqld
sudo apparmor_parser -r /etc/apparmor.d/usr.sbin.mysqld
sudo aa-status
```

Inspect the installed include/profile first. Do not disable AppArmor globally.
Custom data/scratch paths also need matching systemd access rules.

Install Caddy from its [official stable repository](https://caddyserver.com/docs/install#debian-ubuntu-raspbian).
Numeric-IP HTTPS needs ACME certificate-profile support: validate the supplied
configuration rather than assuming an older distribution package supports it.

## 4. Configure emergency swap

**VM:** inspect `swapon --show` and `free -h`. Preflight requires at least 4 GiB
swap. If there is **no existing `/swapfile`** and root has room:

```bash
sudo bash -c 'set -eu; test ! -e /swapfile; test ! -L /swapfile; umask 077; dd if=/dev/zero of=/swapfile bs=1M count=4096 status=progress; chmod 600 /swapfile; mkswap /swapfile; swapon /swapfile'
sudo editor /etc/fstab
```

Add `/swapfile none swap sw 0 0` once. Never overwrite an existing swapfile.
After reviewing other host policy, configure swappiness:

```bash
printf 'vm.swappiness=10\n' | sudo tee /etc/sysctl.d/90-ojs.conf
sudo sysctl -p /etc/sysctl.d/90-ojs.conf
sudo systemctl daemon-reload
swapon --show
free -h
```

Swap is a reserve. Sustained swapping warrants a working-set review, not more
metadata workers on a small host.

## 5. Create private configuration

**VM, fresh installation only:**

```bash
cd /srv/ojs_api
sudo -u ojs ./scripts/generate_api_credentials.sh
sudo -u ojs -H bash -c 'set -eu; cd /srv/ojs_api; test ! -e .env; test ! -L .env; umask 077; cp .env.example .env'
sudo -u ojs chmod 700 .secrets
sudo -u ojs chmod 600 .env
id -u ojs
id -g ojs
sudo -u ojs editor .env
```

Set the following privately; never paste the completed file into an issue:

- `MYSQL_ROOT_PASSWORD`: new long random URL-safe secret from a password manager.
- `OJS_ADMIN_EMAIL`: monitored administrator address.
- `OJS_PUBLIC_BASE_URL`: `https://YOUR_STATIC_IP` or `https://YOUR_HOSTNAME`,
  without a path, trailing slash or `/32`.
- `OJS_MYSQL_IMAGE`: exact installed/candidate MySQL patch.
- `OJS_HOST_UID`/`OJS_HOST_GID`: the account values printed above.

Keep the low-memory defaults and resource guards. `.env` is consumed by Bash,
Compose and systemd: use simple assignments and URL-safe generated secrets,
not shell substitutions. Credential generation refuses overwrites; do not
regenerate files during an upgrade.

Obtain an authorised current source credential and create its private INI:

```bash
sudo -u ojs editor /srv/ojs_api/.secrets/beacon.ini
sudo -u ojs chmod 600 /srv/ojs_api/.secrets/beacon.ini
```

```ini
[beacon]
username = YOUR_BEACON_USERNAME
password = YOUR_CURRENT_BEACON_PASSWORD
```

The password belongs here, not in an SQL dump, command arguments or Git.
Preflight currently requires this file even for import-only deployments.
The source check authenticates without downloading an archive:

```bash
sudo -u ojs -H bash -lc 'cd /srv/ojs_api && make lightsail-preflight'
sudo -u ojs -H bash -lc 'cd /srv/ojs_api && make check-source'
sudo -u ojs -H bash -lc 'cd /srv/ojs_api && docker compose config --quiet && docker compose pull mysql && docker compose build ojs-api'
```

Use `docker compose config --quiet`: rendered configuration can expose secrets.
See [configuration details](docs/CONFIGURATION.md).

## 6. Configure HTTPS and service templates

**VM:** choose one public origin; a custom domain is optional. For a numeric
static IP, copy the template on a fresh host and replace its placeholders:

```bash
sudo install -o root -g root -m 644 /srv/ojs_api/deploy/Caddyfile.ip.example /etc/caddy/Caddyfile
sudo editor /etc/caddy/Caddyfile
```

Replace `PUBLIC_IP` and `YOUR_EMAIL`. The template uses Let's Encrypt's
`shortlived` profile and HTTP-01 validation. Keep ports 80/443 publicly available
for renewal/clients. `default_sni` supports IP clients that omit SNI.
[IP certificates](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability),
[Caddy TLS configuration](https://caddyserver.com/docs/caddyfile/directives/tls).

For DNS, create this alternative Caddyfile with your own hostname/email:

```caddyfile
{
    email admin@example.org
}

api.example.org {
    encode gzip
    reverse_proxy 127.0.0.1:8000
    handle_errors {
        header Retry-After "60"
        respond "The catalogue is temporarily unavailable. Please try again later." 503
    }
}
```

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl enable --now caddy
sudo systemctl reload caddy
sudo journalctl -u caddy -n 50 --no-pager
```

Check trust from another machine without `curl -k`. A 503 is expected before
publication and is not API readiness. Keep Caddy state under `/var/lib/caddy`
persistent and monitor renewal failures. Never send credentials over public HTTP.

Install the bounded updater but **do not start or schedule it yet**:

```bash
cd /srv/ojs_api
sudo install -o root -g root -m 644 deploy/ojs-api-update.service /etc/systemd/system/ojs-api-update.service
sudo systemctl daemon-reload
sudo systemctl cat ojs-api-update.service
```

Review the existing logging policy before optionally installing
`deploy/journald-ojs-api.conf` as `/etc/systemd/journald.conf.d/60-ojs-api.conf`.
It applies to the **entire default journal**: persistent storage, 128 MiB
persistent/32 MiB runtime budgets and up to 14 days' retention. Restart
`systemd-journald` and run `sudo journalctl --flush` after installing. Verify
effective settings instead of assuming logs survive reboots.

## 7. Publish the catalogue

Choose **A or B**, not both. Never initialise Compose against empty data paths.

### A. Transfer a completed release

The source needs a cleanly stopped physical MySQL directory, latest clean
export and all history reports/manifests. The helper copies no raw archives or
old incomplete databases. It checks provenance/checksums, uses resumable
incoming-only writes, adopts the VM's credentials and queues publication.
Both machines need the same reviewed repository revision, `rsync`, Python 3
and matching MySQL patch.

If you only have raw dumps, first build on the adequately sized machine using
`src/run_pipeline.py --skip-download --rebuild-history --compact-storage` with
explicit raw/clean paths and its private environment. Include every desired
snapshot chronologically. Do not use `--force-rebuild` on valuable outputs.
Production credentials need not be copied to that builder.

**Desktop:** establish SSH access, matching the server host-key fingerprint
through the Lightsail browser terminal first. The helper requires strict
known-host verification, noninteractive SSH, and `ubuntu` noninteractive `sudo`
for the receiver/publication commands. Never bypass host-key checking.

From the local repository, replace paths/host/origin and enter the complete
ordered snapshot dates:

```bash
OJS_LOCAL_REPO="$PWD"
OJS_SOURCE_CLEAN=/absolute/path/to/completed/clean
OJS_SOURCE_RAW=/absolute/path/to/source/raw
OJS_BUILD_ENV=/absolute/path/to/private/builder.env
OJS_SSH_IDENTITY=/absolute/path/to/private/ssh-key
OJS_SSH_HOST=ubuntu@YOUR_STATIC_IP
OJS_ORIGIN=https://YOUR_STATIC_IP
read -r -a OJS_HISTORY -p 'All snapshot dates, oldest first, separated by spaces: '
OJS_DATE_ARGS=()
for OJS_DATE in "${OJS_HISTORY[@]}"; do
    OJS_DATE_ARGS+=(--history-snapshot "$OJS_DATE")
done
OJS_LATEST="${OJS_HISTORY[-1]}"

if test -n "${SSH_AUTH_SOCK:-}"; then
    systemctl --user import-environment SSH_AUTH_SOCK
fi

systemd-run --user --unit=ojs-release-deploy \
  --working-directory="$OJS_LOCAL_REPO" \
  --property=Type=exec --property=KillMode=mixed \
  --property=TimeoutStopSec=120 --property=UMask=0077 \
  --setenv=PYTHONDONTWRITEBYTECODE=1 --setenv=PYTHONUNBUFFERED=1 \
  /usr/bin/systemd-inhibit --what=sleep:idle --who=ojs-api-handoff \
  --why='Transfer a completed catalogue and queue API publication' --mode=block \
  /usr/bin/python3 -B "$OJS_LOCAL_REPO/scripts/deploy_completed_release.py" \
  --source-clean "$OJS_SOURCE_CLEAN" --source-raw "$OJS_SOURCE_RAW" \
  --credentials-env "$OJS_BUILD_ENV" --identity-file "$OJS_SSH_IDENTITY" \
  --host "$OJS_SSH_HOST" --snapshot "$OJS_LATEST" "${OJS_DATE_ARGS[@]}" \
  --public-base-url "$OJS_ORIGIN" --reserve-gib 20

journalctl --user -fu ojs-release-deploy.service
```

Repeat `--source-raw` for additional source directories whose workflow locks
must be held. The builder environment must be mode 600 or stricter and contain
`MYSQL_ROOT_PASSWORD`; only that secret travels over SSH stdin, never argv or
logs. For a provably native-only source that was never container-managed,
`--native-only-source` may be added after review; never use it to bypass unknown
container references.

Keep the desktop powered, logged in and connected until `publication_started`.
The remote publisher then runs independently. Closing the log view does not
stop the job. That event means **queued**, not verified. On **VM**, substitute
the latest date:

```bash
sudo journalctl -fu ojs-api-publish-YYYY-MM-DD.service
sudo systemctl show ojs-api-publish-YYYY-MM-DD.service -p ActiveState -p SubState -p Result -p ExecMainStatus
```

Wait for final JSON `status: passed`, then perform step 8. After interruption,
inspect remote publication and incoming state before retrying: the chain refuses
to overwrite installed/adopted releases. `--finish-only` is solely for
interrupted transfer installation, not a universal retry flag. Builds remain
paused and no old artifacts are pruned by this handoff.

### B. Build on the VM after capacity approval

Only use this route with measured or conservatively budgeted full-build peak.
Preflight alone is insufficient. The updater downloads the available upstream
snapshot and processes eligible local snapshots; it does not fetch a historical
archive. Resolve existing failed candidates before starting another run.

**VM:** after approving capacity, remove only the deliberate pause marker:

```bash
sudo -u ojs -H bash -lc 'set -eu; cd /srv/ojs_api; test -f data/.updates-paused; test ! -L data/.updates-paused; rm -- data/.updates-paused'
sudo systemctl start --no-block ojs-api-update.service
sudo journalctl -fu ojs-api-update.service -o short-iso
```

This runs download, validation, wrangling, reports/guards and publication, not
just a source check. The old API remains live until the short publication restart,
if one exists. A first deployment serves no data until the initial release
passes. No daily scheduler is needed to finish this single run.

## 8. Acceptance checks and client access

On **VM**, replace the public hostname/IP and every expected snapshot date:

```bash
sudo -u ojs -H bash -lc 'cd /srv/ojs_api && docker compose ps'
curl --fail --show-error http://127.0.0.1:8000/health
sudo -u ojs python3 -B /srv/ojs_api/scripts/check_api_history.py \
  --base-url https://YOUR_HOSTNAME_OR_IP \
  --credentials-file /srv/ojs_api/.secrets/api-client.env \
  --reports-dir /srv/ojs_api/data/clean \
  --snapshot YYYY-MM-DD --timeout 120
```

Repeat `--snapshot` for every retained date. This bounded test verifies anonymous
rejection, catalogue endpoints, provenance and delta boundaries; it is not a
row-by-row replacement for prepublication validation. Adjust the loopback port
if `OJS_API_PORT` changed.

Request the HTTPS health endpoint from another machine with normal certificate
validation. Review free space, logs, service results and backup recoverability.
Share credentials through an approved secret channel. [HOW_TO_CALL.md](HOW_TO_CALL.md)
documents authentication, articles, pagination and changes.

## 9. Enable routine operation deliberately

An imported release can serve with automatic builds paused. Do not enable new
builds solely because the first API request works. Establish next-build capacity
and backup readiness, then follow [daily scheduling](docs/OPERATIONS.md#daily-scheduling).
Use cron **or** the optional timer, not both. Shared repository locks prevent
downloads during managed wrangling.

Use [the operations runbook](docs/OPERATIONS.md) for logs, failures, rollback,
retention, backups and upgrades. Dated recovery/archive-cleanup examples are
not general-purpose hooks: review their exact targets independently.
