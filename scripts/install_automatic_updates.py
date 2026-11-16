#!/usr/bin/env python3
"""Prepare the standard /srv/ojs_api updater without enabling any work.

Existing configuration is never overwritten. In particular, recovery drop-ins
and a previously parked cron file require operator review, not silent repair.
No dotenv files or credentials are read, and no updater/cron service is started.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys


class InstallationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Layout:
    # The filesystem root is injectable only by tests, never through the CLI.
    root: Path = Path("/")

    @property
    def project(self) -> Path:
        return self.root / "srv/ojs_api"

    @property
    def service(self) -> Path:
        return self.root / "etc/systemd/system/ojs-api-update.service"

    @property
    def parked_cron(self) -> Path:
        return self.root / "etc/ojs-api-cron.paused"

    @property
    def active_cron(self) -> Path:
        return self.root / "etc/cron.d/ojs-api"

    @property
    def pause(self) -> Path:
        return self.project / "data/.updates-paused"


class Installer:
    def __init__(self, layout: Layout = Layout(), *, root_uid: int = 0,
                 root_gid: int = 0, run=subprocess.run):
        self.layout = layout
        self.root_uid = root_uid
        self.root_gid = root_gid
        self.run = run

    def directory(self, path: Path, owners: set[int]) -> None:
        """Reject symlink ancestors and writable/unowned deployment paths."""
        try:
            relative = path.relative_to(self.layout.root)
            chain = [self.layout.root]
            for component in relative.parts:
                chain.append(chain[-1] / component)
            for directory in chain:
                info = directory.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in owners
                        or stat.S_IMODE(info.st_mode) & 0o022):
                    raise InstallationError(f"unsafe directory ownership or permissions: {directory}")
        except (ValueError, OSError):
            raise InstallationError(f"required safe directory is unavailable: {path}") from None

    def regular_file(self, path: Path, owners: set[int]) -> bytes | None:
        """Read bounded regular files without following links or printing data."""
        descriptor = None
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid not in owners or stat.S_IMODE(info.st_mode) & 0o022
                    or info.st_size > 64 * 1024):
                raise InstallationError(f"unsafe or unexpectedly large configuration file: {path}")
            with os.fdopen(descriptor, "rb") as source:
                descriptor = None
                return source.read(64 * 1024 + 1)
        except FileNotFoundError:
            return None
        except OSError:
            raise InstallationError(f"configuration file cannot be safely read: {path}") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def unit_state(self, name: str) -> dict[str, str]:
        try:
            result = self.run([
                "/usr/bin/systemctl", "show", name, "--no-pager",
                "--property=LoadState,ActiveState,UnitFileState",
            ], text=True, capture_output=True, timeout=20, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise InstallationError("could not safely inspect updater scheduling") from None
        if result.returncode:
            raise InstallationError("could not safely inspect updater scheduling")
        state = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if state.get("LoadState") not in {"loaded", "not-found", "masked"}:
            raise InstallationError("unrecognized updater unit state; inspect systemd before installation")
        return state

    def require_idle_unscheduled(self) -> None:
        updater = self.unit_state("ojs-api-update.service")
        if updater.get("ActiveState") not in {"inactive", "failed"}:
            raise InstallationError("updater is active or changing state; no configuration was changed")
        timer = self.unit_state("ojs-api-update.timer")
        if (timer.get("ActiveState") not in {"inactive", "failed"}
                or timer.get("UnitFileState", "") not in {"", "disabled", "static", "masked", "masked-runtime"}):
            raise InstallationError("update timer is active or enabled; review it before installing cron")
        # lexists also catches a broken link; do not assume a broken scheduler
        # is harmless or replace operator-owned configuration.
        if os.path.lexists(self.layout.active_cron):
            raise InstallationError("active cron configuration already exists; it was left unchanged")

    def create(self, path: Path, contents: bytes, *, uid: int, gid: int, mode: int) -> None:
        descriptor = None
        identity = None
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            opened = os.fstat(descriptor)
            identity = opened.st_dev, opened.st_ino
            os.fchown(descriptor, uid, gid)
            with os.fdopen(descriptor, "wb") as destination:
                descriptor = None
                destination.write(contents)
                destination.flush()
                os.fsync(destination.fileno())
                os.fchmod(destination.fileno(), mode)
        except OSError:
            # Remove only the file this invocation exclusively created.
            if identity is not None:
                try:
                    current = path.lstat()
                    if (current.st_dev, current.st_ino) == identity:
                        path.unlink()
                except OSError:
                    pass
            raise InstallationError(f"could not exclusively create configuration: {path}") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def prepare(self) -> dict[str, str | bool]:
        if os.geteuid() != self.root_uid:
            raise InstallationError("run this preparation script as root")
        try:
            account = pwd.getpwnam("ojs")
        except KeyError:
            raise InstallationError("the deployment account 'ojs' must already exist") from None
        if account.pw_uid == 0:
            raise InstallationError("the deployment account must not be root")
        root_owners = {self.root_uid}
        project_owners = {self.root_uid, account.pw_uid}
        self.directory(self.layout.service.parent, root_owners)
        self.directory(self.layout.parked_cron.parent, root_owners)
        self.directory(self.layout.active_cron.parent, root_owners)
        self.directory(self.layout.project / "deploy", project_owners)
        self.directory(self.layout.pause.parent, project_owners)
        self.require_idle_unscheduled()

        sources = {
            self.layout.service: self.layout.project / "deploy/ojs-api-update.service",
            self.layout.parked_cron: self.layout.project / "deploy/ojs-api.cron",
        }
        pending = []
        states: dict[str, str | bool] = {}
        for destination, source in sources.items():
            expected = self.regular_file(source, project_owners)
            if not expected:
                raise InstallationError(f"required deployment template is absent or empty: {source}")
            actual = self.regular_file(destination, root_owners)
            if actual is not None and actual != expected:
                raise InstallationError(f"existing configuration differs; refusing to overwrite: {destination}")
            states["service" if destination == self.layout.service else "parked_cron"] = (
                "already matching" if actual is not None else "created"
            )
            if actual is None:
                pending.append((destination, expected))
        pause = self.regular_file(self.layout.pause, project_owners)

        # Validate the whole plan before writing. Pause precedes all installs;
        # an interrupted preparation can never accidentally enable processing.
        if pause is None:
            self.create(self.layout.pause, b"", uid=account.pw_uid,
                        gid=account.pw_gid, mode=0o600)
        states["pause"] = "preserved" if pause is not None else "created"
        for destination, expected in pending:
            self.create(destination, expected, uid=self.root_uid,
                        gid=self.root_gid, mode=0o644)
        if any(path == self.layout.service for path, _ in pending):
            try:
                result = self.run(["/usr/bin/systemctl", "daemon-reload"],
                                  text=True, capture_output=True, timeout=30, check=False)
            except (OSError, subprocess.TimeoutExpired):
                raise InstallationError("files are staged and paused, but systemd reload failed; inspect before activation") from None
            if result.returncode:
                raise InstallationError("files are staged and paused, but systemd reload failed; inspect before activation")
        states["enabled"] = False
        return states


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)  # Intentionally no --enable or path override.
    try:
        result = Installer().prepare()
    except InstallationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Prepared only: service {result['service']}; cron parked ({result['parked_cron']}); pause {result['pause']}.")
    print("Existing service drop-ins were preserved. No job was started and no scheduler was enabled.")
    print("Before activation, run scripts/automatic_update.sh --readiness as ojs and review service overrides and capacity.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
