#!/usr/bin/env python3
"""Replace only the API with an already-local image after publication finishes.

Run as the deployment account. This does not install source, build/pull images,
alter data, restart MySQL, prune artifacts or resume automatic updates. Keep
the previous image's rollback tag until the application has been accepted.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import uuid

import check_api_history as api_check
import transfer_completed_release as transfer


class RolloutError(RuntimeError):
    """Fixed diagnostics only; never include subprocess output or settings."""


class Runner:
    def __init__(self, root: Path, descriptors: tuple[int, ...]):
        self.root, self.descriptors = root, descriptors
        # The project's private .env is authoritative, not a caller's exported
        # database password or an unrelated Compose project's overrides.
        self.environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("OJS_", "COMPOSE_"))
            and key not in ("MYSQL_ROOT_PASSWORD", "MYSQL_PWD")
        }

    def run(self, command: list[str], timeout: float = 30) -> str:
        try:
            result = subprocess.run(
                command, cwd=self.root, env=self.environment, pass_fds=self.descriptors,
                text=True, capture_output=True, timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise RolloutError("A rollout command failed or timed out; command details are private.") from None
        if result.returncode:
            raise RolloutError("A rollout command failed; command details are private.")
        if len(result.stdout) > 1024 * 1024:
            raise RolloutError("A rollout response exceeds the allowed size.")
        return result.stdout.strip()


def regular_file(path: Path, *, private: bool = False) -> None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.resolve(strict=True) != path.absolute():
        raise RolloutError("A required deployment file is missing, symlinked or unsafe.")
    if private and (info.st_mode & 0o077 or info.st_uid not in (0, os.geteuid())):
        raise RolloutError("The deployment environment must be owner-private.")


def preconditions(root: Path) -> None:
    if not root.is_dir() or root.resolve(strict=True) != root:
        raise RolloutError("Use an existing canonical project directory.")
    regular_file(root / ".env", private=True)
    regular_file(root / "docker-compose.yml")
    regular_file(root / "data/.updates-paused")
    if os.path.lexists(root / "data/clean/.compact-update.json"):
        raise RolloutError("Publication recovery is pending; do not update the application yet.")
    for directory in (root / "data", root / "data/raw", root / "data/clean"):
        if not directory.is_dir() or directory.resolve(strict=True) != directory:
            raise RolloutError("Deployment data directories must exist without symlink ancestors.")
    for name in ("data/raw/.source-activity.lock", "data/clean/.automatic-update.lock",
                 "data/clean/.pipeline.lock", "data/clean/.publish-live.lock"):
        regular_file(root / name)
    live = root / "data/clean/mysql-live"
    if not live.is_symlink():
        raise RolloutError("No managed live release is available.")
    target = live.resolve(strict=True)
    if target.parent != root / "data/clean" or not re.fullmatch(r"mysql-\d{4}-\d{2}-\d{2}", target.name):
        raise RolloutError("The live database pointer is invalid.")


def image_id(runner: Runner, reference: str) -> str:
    value = runner.run(["docker", "image", "inspect", "--format", "{{.Id}}", reference])
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise RolloutError("The requested local image could not be verified.")
    return value


def container(runner: Runner, identifier: str) -> dict:
    fields = (".Id", ".Image", ".Config.Image", ".State.StartedAt", ".State.Running",
              ".State.Health.Status", 'index .Config.Labels "com.docker.compose.service"')
    template = "\n".join("{{json " + field + "}}" for field in fields)
    values = [json.loads(line) for line in runner.run(
        ["docker", "inspect", "--format", template, identifier]).splitlines()]
    if len(values) != len(fields):
        raise RolloutError("A serving container could not be inspected safely.")
    result = dict(zip(("id", "image", "tag", "started", "running", "health", "service"), values))
    if result["running"] is not True or result["health"] != "healthy":
        raise RolloutError("The existing deployment is not healthy; finish publication first.")
    return result


def api_settings(configuration: dict) -> tuple[list[str], Path]:
    service = configuration["services"]["ojs-api"]
    settings = service["environment"]
    public = api_check.origin(settings["OJS_PUBLIC_BASE_URL"])
    if not public.startswith("https://"):
        raise RolloutError("Public API checks require trusted HTTPS.")
    ports = [port for port in service["ports"] if int(port["target"]) == 8000]
    if (len(ports) != 1 or ports[0].get("host_ip") != "127.0.0.1"
            or ports[0].get("protocol", "tcp") != "tcp"):
        raise RolloutError("The API must have one loopback-only published port.")
    port = int(ports[0]["published"])
    if not 1 <= port <= 65535:
        raise RolloutError("The API port is invalid.")
    mounts = [mount for mount in service["volumes"]
              if mount.get("target") == settings["OJS_API_CREDENTIALS_FILE"]]
    if len(mounts) != 1 or mounts[0].get("type") != "bind" or mounts[0].get("read_only") is not True:
        raise RolloutError("The API credential mount must be a private read-only bind.")
    credentials = Path(mounts[0]["source"])
    api_check.credentials(credentials)
    return [f"http://127.0.0.1:{port}", public], credentials


def check_api(runner: Runner, origins: list[str], credentials: Path,
              snapshots: list[str], timeout: float) -> list[dict]:
    identity = api_check.credentials(credentials)
    observed = []
    for origin in origins:
        client = api_check.Client(origin, identity, min(timeout, 15))
        if client.get("/health", authenticated=False).get("status") != "ok":
            raise RolloutError("An API health check failed.")
        client.get("/meta", authenticated=False, expected=401)
        metadata = client.get("/meta")
        if not isinstance(metadata.get("latest_snapshot"), dict):
            raise RolloutError("Authenticated API snapshot metadata is invalid.")
        api_check.number(metadata.get("high_watermark_event_id"))
        observed.append(metadata)
        if snapshots:
            command = [sys.executable, "-B", str(Path(__file__).with_name("check_api_history.py")),
                       "--base-url", origin, "--credentials-file", str(credentials),
                       "--reports-dir", str(runner.root / "data/clean"), "--timeout", "15"]
            for snapshot in snapshots:
                command.extend(("--snapshot", snapshot))
            if json.loads(runner.run(command, timeout=timeout)).get("status") != "passed":
                raise RolloutError("The bounded API history check did not pass.")
    if observed[0] != observed[1]:
        raise RolloutError("Loopback and public API metadata differ.")
    return observed


@contextmanager
def ignoring_interrupts():
    previous = {number: signal.signal(number, signal.SIG_IGN)
                for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def perform_rollout(args, runner: Runner) -> dict:
    root = runner.root
    compose = ["docker", "compose", "--project-directory", str(root),
               "--env-file", str(root / ".env"), "--file", str(root / "docker-compose.yml")]
    runner.run(compose + ["config", "--quiet"])
    configuration = json.loads(runner.run(compose + ["config", "--format", "json"]))
    origins, credentials = api_settings(configuration)
    states = {}
    for service in ("mysql", "ojs-api"):
        identifiers = runner.run(compose + ["ps", "--quiet", service]).split()
        if len(identifiers) != 1:
            raise RolloutError("Exactly one healthy container per service is required.")
        states[service] = container(runner, identifiers[0])
        if states[service]["service"] != service:
            raise RolloutError("A serving container has an unexpected service identity.")
    original = states["ojs-api"]
    expected_tag = configuration["services"]["ojs-api"].get("image") or configuration["name"] + "-ojs-api"
    if original["tag"] != expected_tag or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,511}", expected_tag):
        raise RolloutError("The running API image reference differs from the current Compose configuration.")
    if "@" in expected_tag or expected_tag.startswith("sha256:"):
        raise RolloutError("An immutable Compose image reference needs a separately reviewed configuration update.")
    selected = image_id(runner, args.prebuilt_image)
    baseline = check_api(runner, origins, credentials, args.snapshot, args.health_timeout)

    def verify_mysql():
        current_ids = runner.run(compose + ["ps", "--quiet", "mysql"]).split()
        if current_ids != [states["mysql"]["id"]]:
            raise RolloutError("MySQL container identity changed unexpectedly; manual investigation is required.")
        current = container(runner, current_ids[0])
        if any(current[key] != states["mysql"][key] for key in ("id", "started", "image")):
            raise RolloutError("MySQL changed unexpectedly; manual investigation is required.")

    def verify_api(expected_image):
        identifiers = runner.run(compose + ["ps", "--quiet", "ojs-api"]).split()
        if len(identifiers) != 1 or container(runner, identifiers[0])["image"] != expected_image:
            raise RolloutError("The selected API image is not healthy and serving.")
        verify_mysql()
        preconditions(root)
        if check_api(runner, origins, credentials, args.snapshot, args.health_timeout) != baseline:
            raise RolloutError("Authenticated catalogue metadata changed during the application-only rollout.")

    if selected == original["image"]:
        verify_api(selected)
        return {"status": "unchanged", "mysql_unchanged": True, "updates_paused": True}
    verify_mysql()
    preconditions(root)
    rollback_tag = "ojs-api-rollback:" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12]
    runner.run(["docker", "image", "tag", original["image"], rollback_tag])
    up = compose + ["up", "--detach", "--no-deps", "--no-build", "--pull", "never",
                    "--force-recreate", "--wait", "--wait-timeout", str(math.ceil(args.health_timeout)), "ojs-api"]
    try:
        runner.run(["docker", "image", "tag", selected, expected_tag])
        runner.run(up, timeout=args.health_timeout + 60)
        verify_api(selected)
    except BaseException:
        with ignoring_interrupts():
            try:
                runner.run(["docker", "image", "tag", rollback_tag, expected_tag])
                runner.run(up, timeout=args.health_timeout + 60)
                verify_api(original["image"])
            except BaseException:
                raise RolloutError("API-only rollout and verified rollback failed; updates remain paused. Inspect the private deployment before retrying.") from None
        raise RolloutError("API-only rollout failed; the previous API image was restored and verified. Updates remain paused.") from None
    return {"status": "updated", "mysql_unchanged": True, "updates_paused": True,
            "history_checked": bool(args.snapshot), "rollback_image_retained": True}


def rollout(args) -> dict:
    root = args.project_root.expanduser().absolute()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,511}", args.prebuilt_image):
        raise RolloutError("Specify an explicit local image reference without shell syntax.")
    if not math.isfinite(args.health_timeout) or not 10 <= args.health_timeout <= 900:
        raise RolloutError("Health timeout must be between 10 and 900 seconds.")
    if len(args.snapshot) > 32 or args.snapshot != sorted(set(args.snapshot)):
        raise RolloutError("Snapshot dates must be ordered, unique and no more than 32.")
    preconditions(root)
    with transfer.transfer_locks(root / "data/clean", [root / "data/raw"], receiver=True) as descriptors:
        preconditions(root)
        return perform_rollout(args, Runner(root, descriptors))


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--project-root", type=Path, required=True)
    result.add_argument("--prebuilt-image", required=True)
    result.add_argument("--snapshot", type=api_check.snapshot_date, action="append", default=[],
                        help="repeat for the complete ordered published history to verify boundary events")
    result.add_argument("--health-timeout", type=float, default=180)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    previous = {}

    def interrupted(signum, frame):
        raise RolloutError("Application rollout interrupted.")

    try:
        for number in (signal.SIGINT, signal.SIGTERM):
            previous[number] = signal.signal(number, interrupted)
        print(json.dumps(rollout(args), sort_keys=True))
        return 0
    except RolloutError as error:
        print(f"error: {error}", file=sys.stderr)
    except BaseException:
        print("error: API-only rollout could not proceed; private settings and command output were suppressed.", file=sys.stderr)
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
