#!/usr/bin/env python3
"""Install a private live-API reporting origin; never read API keys or change services."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys

from configure_progress_email import ConfigurationError, open_config_directory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from progress_api import ProbeError, validated_origin


ENVIRONMENT_FILE = "progress-api.env"
MAX_CONFIG_BYTES = 4096


def configuration_payload(value: str) -> bytes:
    try:
        origin = validated_origin(value)
    except ProbeError:
        raise ConfigurationError("API origin must be a valid HTTPS origin without credentials, path, query or fragment.") from None
    return f"OJS_PROGRESS_API_BASE_URL={origin}\n".encode("ascii")


def existing_matches(directory: int, payload: bytes) -> bool:
    """Return false only if absent; otherwise require a private identical file."""
    try:
        information = os.stat(ENVIRONMENT_FILE, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if (not stat.S_ISREG(information.st_mode) or information.st_uid != 0
            or information.st_gid != 0 or stat.S_IMODE(information.st_mode) != 0o600
            or information.st_nlink != 1 or information.st_size > MAX_CONFIG_BYTES):
        raise ConfigurationError("Existing API reporting configuration must be a single regular root:root mode 0600 file.")
    descriptor = os.open(ENVIRONMENT_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                         | os.O_CLOEXEC, dir_fd=directory)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (information.st_dev, information.st_ino):
            raise ConfigurationError("API reporting configuration changed during verification; retry after inspection.")
        if os.read(descriptor, MAX_CONFIG_BYTES + 1) != payload:
            raise ConfigurationError("A different API reporting configuration exists; refusing to overwrite it.")
    finally:
        os.close(descriptor)
    return True


def store_configuration(directory: int, payload: bytes) -> bool:
    if existing_matches(directory, payload):
        return False
    descriptor = None
    try:
        descriptor = os.open(ENVIRONMENT_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=directory)
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o600)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("Could not write API reporting configuration.")
            remaining = remaining[written:]
        os.fsync(descriptor)
        os.fsync(directory)
        return True
    except BaseException:
        if descriptor is not None:
            try:
                existing = os.stat(ENVIRONMENT_FILE, dir_fd=directory, follow_symlinks=False)
                created = os.fstat(descriptor)
                if (existing.st_dev, existing.st_ino) == (created.st_dev, created.st_ino):
                    os.unlink(ENVIRONMENT_FILE, dir_fd=directory)
                    os.fsync(directory)
            except OSError:
                pass
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Public HTTPS origin; not a credential.")
    args = parser.parse_args(argv)
    previous_umask = os.umask(0o077)
    directory = None
    try:
        if os.geteuid() != 0:
            raise ConfigurationError("Run this configuration command as root with sudo.")
        payload = configuration_payload(args.base_url)
        directory = open_config_directory()
        created = store_configuration(directory, payload)
        print("Saved private API reporting configuration." if created
              else "Existing API reporting configuration already matches.")
        print("No credentials were read, requests made, emails sent, or services changed.")
        return 0
    except ConfigurationError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (Exception, KeyboardInterrupt):
        print("error: API reporting configuration failed; no existing files were overwritten.", file=sys.stderr)
        return 1
    finally:
        if directory is not None:
            os.close(directory)
        os.umask(previous_umask)


if __name__ == "__main__":
    raise SystemExit(main())
