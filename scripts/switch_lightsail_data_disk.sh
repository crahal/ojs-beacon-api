#!/usr/bin/env bash
# Switch an offline deployment to an already formatted, explicitly identified disk.
# Never format, copy, delete a data disk, or start a database.
set -Eeuo pipefail
trap 'printf "STOP: storage switch failed on line %s; inspect mounts before retrying.\n" "$LINENO" >&2' ERR

if (( EUID != 0 )) || (( $# != 5 )); then
    echo 'usage (root): switch_lightsail_data_disk.sh DEVICE NEW_UUID NEW_SERIAL OLD_UUID FSTAB_BACKUP' >&2
    exit 2
fi
OJS_DEVICE=$(readlink -f -- "$1")
OJS_NEW_UUID=$2
OJS_SERIAL=$3
OJS_OLD_UUID=$4
OJS_BACKUP=$5
OJS_ROOT=/srv/ojs_api
OJS_GUARD=/etc/systemd/system/ojs-api-update.service.d/zzzzzz-storage-migration.conf
cd "$OJS_ROOT"
[[ "$OJS_DEVICE" =~ ^/dev/nvme[0-9]+n[0-9]+$ ]]
[[ "$OJS_NEW_UUID" =~ ^[0-9a-f-]{36}$ && "$OJS_OLD_UUID" =~ ^[0-9a-f-]{36}$ ]]
[[ "$OJS_SERIAL" =~ ^vol[0-9a-f]+$ ]]
[[ "$OJS_BACKUP" =~ ^/etc/fstab\.ojs-before-512-[0-9]{8}$ ]]
test "$OJS_NEW_UUID" != "$OJS_OLD_UUID"
test -b "$OJS_DEVICE"
test ! -e "$OJS_BACKUP" && test ! -L "$OJS_BACKUP"
udevadm settle
test "$(blockdev --getsize64 "$OJS_DEVICE")" = 549755813888
test "$(blockdev --getro "$OJS_DEVICE")" = 0
test "$(lsblk -dnro TYPE "$OJS_DEVICE")" = disk
test "$(lsblk -dnro SERIAL "$OJS_DEVICE" | tr -d '[:space:]-')" = "$OJS_SERIAL"
test "$(blkid -s UUID -o value "$OJS_DEVICE")" = "$OJS_NEW_UUID"
test "$(blkid -s TYPE -o value "$OJS_DEVICE")" = ext4
test "$(blkid -s LABEL -o value "$OJS_DEVICE")" = ojs-data-512
OJS_MOUNTS=$(lsblk -dnro MOUNTPOINTS "$OJS_DEVICE")
test -z "$OJS_MOUNTS"
test "$(findmnt -rn -o UUID --mountpoint "$OJS_ROOT/data")" = "$OJS_OLD_UUID"
test "$(awk '$1 !~ /^#/ && $2=="/srv/ojs_api/data" {n++} END {print n+0}' /etc/fstab)" = 1
test "$(awk '$1 !~ /^#/ && $2=="/srv/ojs_api/data" {print $1}' /etc/fstab)" = "UUID=$OJS_OLD_UUID"

# This deployment's cron entry was moved here, not replaced with an empty flag.
# Preserve its contents. A different server must establish its own paused schedule.
test ! -e /etc/cron.d/ojs-api && test ! -L /etc/cron.d/ojs-api
test -f /etc/ojs-api-cron.paused && test ! -L /etc/ojs-api-cron.paused
OJS_TRIGGERS=$(systemctl show ojs-api-update.service -p TriggeredBy --value)
test -z "$OJS_TRIGGERS"
OJS_REQUIRED_BY=$(systemctl show ojs-api-update.service -p RequiredBy --value)
OJS_WANTED_BY=$(systemctl show ojs-api-update.service -p WantedBy --value)
test -z "$OJS_REQUIRED_BY" && test -z "$OJS_WANTED_BY"

OJS_CONTAINERS=$(docker ps --all --quiet)
if test -n "$OJS_CONTAINERS"; then echo 'STOP: review existing containers before switching storage' >&2; exit 1; fi
OJS_DOCKER_ROOT=$(docker info --format '{{.DockerRootDir}}')
OJS_DOCKER_ROOT=$(readlink -f -- "$OJS_DOCKER_ROOT")
case "$OJS_DOCKER_ROOT" in "$OJS_ROOT/data"|"$OJS_ROOT/data/"*) echo 'STOP: Docker storage is on the old disk' >&2; exit 1;; esac
OJS_REQUIRED_MOUNTS=$(systemctl show docker.service -p RequiresMountsFor --value)
[[ " $OJS_REQUIRED_MOUNTS " == *" $OJS_ROOT/data "* ]]

install -d -o root -g root -m 755 /etc/systemd/system/ojs-api-update.service.d
test ! -L "$OJS_GUARD"
if test -e "$OJS_GUARD"; then
    cmp -- deploy/ojs-api-storage-migration.conf "$OJS_GUARD"
else
    install -o root -g root -m 644 deploy/ojs-api-storage-migration.conf "$OJS_GUARD"
fi
systemctl daemon-reload
test "$(systemctl show ojs-api-update.service -p RefuseManualStart --value)" = yes
OJS_STATE=$(systemctl show ojs-api-update.service -p ActiveState --value)
case "$OJS_STATE" in inactive|failed) ;; *) echo 'STOP: updater is active; not stopping it' >&2; exit 1;; esac
test "$(systemctl show ojs-api-update.service -p MainPID --value)" = 0
OJS_JOBS=$(systemctl list-jobs --no-legend --no-pager)
if [[ "$OJS_JOBS" == *ojs-api-update.service* ]]; then echo 'STOP: updater has a pending job' >&2; exit 1; fi
if pgrep -x mysqld; then echo 'STOP: MySQL is running' >&2; exit 1; else test "$?" = 1; fi
# Do not overwrite any existing pause marker or old filesystem data.
if ! test -e data/.updates-paused; then
    test ! -L data/.updates-paused
    install -o ojs -g ojs -m 600 /dev/null data/.updates-paused
fi
test -f data/.updates-paused && test ! -L data/.updates-paused

cp -a -- /etc/fstab "$OJS_BACKUP"
OJS_FSTAB_TMP=$(mktemp /tmp/ojs-fstab-512.XXXXXXXX)
trap 'rm -f -- "$OJS_FSTAB_TMP"' EXIT
awk -v uuid="$OJS_NEW_UUID" '
  $1 !~ /^#/ && $2=="/srv/ojs_api/data" {
    print "UUID=" uuid " /srv/ojs_api/data ext4 defaults,noatime 0 2"; next
  }
  {print}
' /etc/fstab > "$OJS_FSTAB_TMP"
findmnt --verify --tab-file "$OJS_FSTAB_TMP"
systemctl stop docker.socket docker.service
if pgrep -x mysqld; then echo 'STOP: MySQL is running' >&2; exit 1; else test "$?" = 1; fi
sync
umount "$OJS_ROOT/data"
OJS_UNDERLYING=$(find "$OJS_ROOT/data" -mindepth 1 -maxdepth 1 -print -quit)
if test -n "$OJS_UNDERLYING"; then
    mount "$OJS_ROOT/data"
    echo 'STOP: pre-existing files below the mount need review; old mount restored' >&2
    exit 1
fi
install -o root -g root -m 644 "$OJS_FSTAB_TMP" /etc/fstab
systemctl daemon-reload
if ! mount "$OJS_ROOT/data"; then
    cp -a -- "$OJS_BACKUP" /etc/fstab
    systemctl daemon-reload
    mount "$OJS_ROOT/data"
    echo 'STOP: new mount failed; old fstab and mount restored, Docker left stopped' >&2
    exit 1
fi
test "$(findmnt -rn -o UUID --mountpoint "$OJS_ROOT/data")" = "$OJS_NEW_UUID"
chown ojs:ojs data
install -d -o ojs -g ojs -m 755 data/clean data/raw
install -o ojs -g ojs -m 600 /dev/null data/.updates-paused
findmnt --verify
systemctl start docker.service
OJS_CONTAINERS=$(docker ps --all --quiet)
test -z "$OJS_CONTAINERS"
findmnt "$OJS_ROOT/data"
df -h / "$OJS_ROOT/data"
printf 'Storage switch complete. Old disk intact and unmounted. Backup: %s\n' "$OJS_BACKUP"
echo 'Updater remains blocked; no database has been started.'
