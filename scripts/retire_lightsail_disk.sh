#!/usr/bin/env bash
# Run in an authenticated AWS CloudShell, never on the instance being stopped.
# Two explicit stages: stop/detach/restart, then independently approved deletion.
set -Eeuo pipefail
exec python3 - "$@" <<'PY'
import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


class Refusal(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Refusal(message)


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("stage", choices=("detach", "delete"))
parser.add_argument("--receipt", required=True,
                    help="absolute path in an existing private directory; preserve between stages")
parser.add_argument("--region")
parser.add_argument("--instance")
parser.add_argument("--old-disk")
parser.add_argument("--keep-disk")
parser.add_argument("--keep-uuid")
parser.add_argument("--api-origin")
parser.add_argument("--maintenance-ready", action="store_true",
                    help="acknowledge backups verified, updater stopped/blocked, old filesystem unmounted")
parser.add_argument("--verified-detached-boot-mount-api-history", action="store_true",
                    help="acknowledge independent SSH and authenticated API/history verification after detach")
parser.add_argument("--wait-seconds", type=int, default=600)
args = parser.parse_args()
receipt_path = Path(args.receipt)
record = None


def run(command):
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False)
    # Do not echo command arguments, AWS diagnostics or API bodies into shared logs.
    require(result.returncode == 0, "External command failed; no further action was taken.")
    return result.stdout


def aws(service, action, *options):
    return json.loads(run(["aws", "--region", record["region"], "--output", "json",
                           "--no-cli-pager", "--cli-connect-timeout", "10",
                           "--cli-read-timeout", "30", service, action, *options]))


def valid_origin(origin):
    require(isinstance(origin, str) and re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?", origin),
            "API origin must be an exact HTTPS origin without credentials, path, query or fragment.")
    parsed = urlsplit(origin)
    require(parsed.hostname and 1 <= (443 if parsed.port is None else parsed.port) <= 65535,
            "Invalid API origin hostname or port.")
    require(all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", part)
                for part in parsed.hostname.split(".")), "Invalid API hostname.")


def check_private_path(existing):
    require(receipt_path.is_absolute(), "Receipt path must be absolute.")
    parent = receipt_path.parent
    require(parent.resolve() == parent, "Receipt directory must not contain symlinks.")
    info = parent.stat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and stat.S_IMODE(info.st_mode) == 0o700,
            "Receipt directory must be owned by you and have mode 0700.")
    if existing:
        info = receipt_path.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
                "Receipt must be your regular, unlinked, mode-0600 file.")
    else:
        require(not os.path.lexists(receipt_path), "Receipt already exists; preserve it and inspect the previous run.")


def save(stage, initial=False):
    record["stage"] = stage
    record["updated_at"] = int(time.time())
    check_private_path(existing=not initial)
    payload = json.dumps(record, indent=2) + "\n"
    if initial:
        fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    else:
        with tempfile.NamedTemporaryFile(mode="w", dir=receipt_path.parent,
                                         prefix=".receipt-", delete=False) as output:
            temporary = output.name
            try:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
                os.replace(temporary, receipt_path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)


def get_resources():
    instance = aws("lightsail", "get-instance", "--instance-name", record["instance"])["instance"]
    old = aws("lightsail", "get-disk", "--disk-name", record["old_disk"])["disk"]
    keep = aws("lightsail", "get-disk", "--disk-name", record["keep_disk"])["disk"]
    return instance, old, keep


def validate(resources, old_attached=None, instance_state=None, capture=False):
    instance, old, keep = resources
    account = record["account"]
    zone = instance["location"]["availabilityZone"]
    arns = []
    for key, resource, kind, size in (("instance", instance, "Instance", None),
                                      ("old_disk", old, "Disk", 256),
                                      ("keep_disk", keep, "Disk", 512)):
        require(resource["name"] == record[key], "Resource name changed or does not match the requested target.")
        require(resource["location"]["regionName"] == record["region"]
                and resource["location"]["availabilityZone"] == zone,
                "Resources are not in the requested region and same availability zone.")
        arn = resource["arn"]
        require(isinstance(arn, str) and re.fullmatch(
            rf"arn:aws:lightsail:{re.escape(record['region'])}:{account}:{kind}/[A-Za-z0-9-]+", arn),
            "Unexpected resource ARN, account, region or resource type.")
        require(resource["resourceType"] == kind, "Unexpected resource type.")
        if capture:
            record[key + "_arn"] = arn
        else:
            require(arn == record[key + "_arn"], "Resource ARN changed; refusing a replacement resource.")
        arns.append(arn)
        if size:
            require(type(resource["sizeInGb"]) is int and resource["sizeInGb"] == size
                    and resource["isSystemDisk"] is False, "Unexpected disk size or system disk.")
    require(len(set(arns)) == 3, "Resource identities overlap.")
    require(keep["isAttached"] is True and keep["attachedTo"] == record["instance"]
            and keep["state"] == "in-use", "Retained 512 GiB disk is not attached to the expected instance.")
    if old_attached is True:
        require(old["isAttached"] is True and old["attachedTo"] == record["instance"]
                and old["state"] == "in-use", "Old disk is not attached to the expected instance.")
    elif old_attached is False:
        require(old["isAttached"] is False and not old.get("attachedTo")
                and old["state"] == "available", "Old disk is not detached and available.")
    else:
        require((old["isAttached"] is True and old["attachedTo"] == record["instance"])
                or (old["isAttached"] is False and not old.get("attachedTo")),
                "Old disk attachment changed unexpectedly.")
    if instance_state:
        require(instance["state"]["name"] == instance_state, "Instance is not in the required state.")


def operation(action, target, operation_type, *options):
    reply = aws("lightsail", action, *options)
    operations = reply["operations"]
    require(isinstance(operations, list) and operations, "AWS returned no operation evidence.")
    require(all(item.get("status") in ("NotStarted", "Started", "Succeeded", "Completed")
                for item in operations), "AWS returned a failed or unknown operation.")
    matches = [item for item in operations if item.get("resourceName") == target
               and item.get("operationType") == operation_type]
    require(len(matches) == 1, "AWS did not identify exactly one matching operation.")
    operation_id = matches[0]["id"]
    require(isinstance(operation_id, str) and operation_id, "Missing operation identifier.")
    record["last_operation_id"] = operation_id
    save(record["stage"])
    deadline = time.monotonic() + args.wait_seconds
    while True:
        item = aws("lightsail", "get-operation", "--operation-id", operation_id)["operation"]
        require(item["id"] == operation_id and item["resourceName"] == target
                and item["operationType"] == operation_type
                and item["location"]["regionName"] == record["region"], "Operation identity changed.")
        require(item["status"] in ("NotStarted", "Started", "Succeeded", "Completed"),
                "AWS operation failed or has unknown status; inspect the receipt and console.")
        if item["isTerminal"] is True:
            require(item["status"] in ("Succeeded", "Completed"), "Operation ended unsuccessfully.")
            return
        require(time.monotonic() < deadline, "AWS operation timed out; inspect it before any further action.")
        time.sleep(min(5, max(0, deadline - time.monotonic())))


def wait_resources(old_attached, instance_state):
    deadline = time.monotonic() + args.wait_seconds
    while True:
        resources = get_resources()
        # Identity and retained-disk checks must pass even during transitions.
        validate(resources)
        instance, old, _ = resources
        old_ready = (old["state"] == "in-use" and old["isAttached"] is True) if old_attached else (
            old["state"] == "available" and old["isAttached"] is False and not old.get("attachedTo"))
        if old_ready and instance["state"]["name"] == instance_state:
            validate(resources, old_attached, instance_state)
            return
        require(time.monotonic() < deadline, "Resource transition timed out; old disk was not deleted.")
        time.sleep(min(5, max(0, deadline - time.monotonic())))


def health():
    # -q is first: ignore curlrc. No redirects, proxy, insecure TLS or extra origins.
    result = subprocess.run(["curl", "-q", "--silent", "--show-error", "--fail",
        "--proto", "=https", "--proto-redir", "=https", "--max-redirs", "0",
        "--noproxy", "*", "--connect-timeout", "10", "--max-time", "20",
        "--max-filesize", "1048576", "--write-out", "\n%{http_code}",
        record["api_origin"] + "/health"], text=True, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False)
    if result.returncode:
        return False
    try:
        body, code = result.stdout.rsplit("\n", 1)
        return code == "200" and json.loads(body).get("status") == "ok"
    except (ValueError, AttributeError):
        return False


def wait_deleted(original_old):
    deadline = time.monotonic() + args.wait_seconds
    while True:
        instance = aws("lightsail", "get-instance", "--instance-name", record["instance"])["instance"]
        # CLI auto-pagination must remain enabled: absence is checked in the full list.
        disks = aws("lightsail", "get-disks")["disks"]
        require(isinstance(disks, list), "Invalid disk inventory after deletion.")
        kept = [disk for disk in disks if disk.get("name") == record["keep_disk"]]
        require(len(kept) == 1, "Retained disk missing or ambiguous in the disk inventory.")
        validate((instance, original_old, kept[0]), old_attached=False, instance_state="running")
        remaining = [disk for disk in disks if disk.get("name") == record["old_disk"]
                     or disk.get("arn") == record["old_disk_arn"]]
        if not remaining:
            return
        require(len(remaining) == 1, "Old disk identity became ambiguous after deletion.")
        validate((instance, remaining[0], kept[0]), instance_state="running")
        require(remaining[0]["isAttached"] is False and not remaining[0].get("attachedTo")
                and remaining[0]["state"] in ("available", "deleting"),
                "Old disk has an unexpected state after deletion.")
        require(time.monotonic() < deadline,
                "Deletion is not yet confirmed in the inventory; preserve receipt and inspect AWS.")
        time.sleep(min(5, max(0, deadline - time.monotonic())))


def main():
    global record
    os.umask(0o077)
    os.environ["AWS_PAGER"] = ""
    os.environ["AWS_CLI_AUTO_PROMPT"] = "off"
    require(1 <= args.wait_seconds <= 3600, "Wait must be between 1 and 3600 seconds.")
    if args.stage == "detach":
        require(args.maintenance_ready, "Missing --maintenance-ready; complete the SSH and backup checks first.")
        require(not args.verified_detached_boot_mount_api_history, "Post-detach acknowledgement belongs to the delete stage.")
        require(all((args.region, args.instance, args.old_disk, args.keep_disk,
                     args.keep_uuid, args.api_origin)), "Detach requires all deployment arguments.")
        require(re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", args.region), "Invalid AWS region.")
        for name in (args.instance, args.old_disk, args.keep_disk):
            require(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_-]*", name), "Invalid resource name.")
        require(args.old_disk != args.keep_disk, "Old and retained disk names must differ.")
        require(re.fullmatch(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", args.keep_uuid),
                "Invalid retained filesystem UUID.")
        valid_origin(args.api_origin)
        check_private_path(existing=False)
        record = {"version": 1, "region": args.region, "instance": args.instance,
                  "old_disk": args.old_disk, "keep_disk": args.keep_disk,
                  "keep_uuid": args.keep_uuid, "api_origin": args.api_origin,
                  "created_at": int(time.time())}
    else:
        require(args.verified_detached_boot_mount_api_history,
                "Missing independent post-detach SSH/mount/API/history acknowledgement; old disk remains intact.")
        require(not args.maintenance_ready and not any((args.region, args.instance, args.old_disk,
                args.keep_disk, args.keep_uuid, args.api_origin)), "Delete takes its exact targets only from the receipt.")
        check_private_path(existing=True)
        record = json.loads(receipt_path.read_text())
        require(record["version"] == 1 and record["stage"] == "detached_restarted_healthy",
                "Receipt does not prove successful detach/restart/health; inspect and recover manually.")
        valid_origin(record["api_origin"])

    identity = aws("sts", "get-caller-identity")
    account = identity["Account"]
    require(isinstance(account, str) and re.fullmatch(r"[0-9]{12}", account), "Invalid AWS account identity.")
    if args.stage == "delete":
        require(record["account"] == account, "AWS account differs from the detach receipt.")
        validate(get_resources(), old_attached=False, instance_state="running")
        require(health(), "HTTPS API health failed; old disk remains intact.")
        # Recheck identities and attachments immediately before the irreversible call.
        resources = get_resources()
        validate(resources, old_attached=False, instance_state="running")
        save("delete_requested")
        operation("delete-disk", record["old_disk"], "DeleteDisk",
                  "--disk-name", record["old_disk"], "--no-force-delete-add-ons")
        wait_deleted(resources[1])
        save("deleted")
        print("Old 256 GiB disk deletion completed. Retained 512 GiB disk was never modified.")
        return

    record["account"] = account
    validate(get_resources(), old_attached=True, instance_state="running", capture=True)
    require(health(), "Pre-maintenance HTTPS API health failed; no cloud resources were changed.")
    save("validated", initial=True)
    validate(get_resources(), old_attached=True, instance_state="running")
    print("Stopping the validated instance for disk detachment; receipt saved.", flush=True)
    save("stop_requested")
    operation("stop-instance", record["instance"], "StopInstance", "--instance-name", record["instance"])
    wait_resources(old_attached=True, instance_state="stopped")
    save("stopped")
    validate(get_resources(), old_attached=True, instance_state="stopped")
    save("detach_requested")
    operation("detach-disk", record["old_disk"], "DetachDisk", "--disk-name", record["old_disk"])
    wait_resources(old_attached=False, instance_state="stopped")
    save("detached")
    validate(get_resources(), old_attached=False, instance_state="stopped")
    save("start_requested")
    operation("start-instance", record["instance"], "StartInstance", "--instance-name", record["instance"])
    wait_resources(old_attached=False, instance_state="running")
    save("detached_restarted")
    deadline = time.monotonic() + args.wait_seconds
    while not health():
        require(time.monotonic() < deadline, "Post-restart HTTPS health failed; detached old disk is preserved.")
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    validate(get_resources(), old_attached=False, instance_state="running")
    save("detached_restarted_healthy")
    print("Instance restarted; HTTPS health passed. Old disk is detached and preserved.")
    print("Now independently verify SSH boot, retained UUID mount, API authentication and history.")
    print("Only then run the separate delete stage with this receipt and its explicit acknowledgement.")


try:
    main()
except (Refusal, OSError, KeyError, TypeError, ValueError, KeyboardInterrupt) as error:
    message = str(error) if isinstance(error, Refusal) else "Command interrupted or invalid/missing evidence."
    print("STOP: " + message, file=sys.stderr)
    print("No automatic recovery or further deletion will run. Preserve the receipt and inspect AWS state.", file=sys.stderr)
    sys.exit(1)
PY
