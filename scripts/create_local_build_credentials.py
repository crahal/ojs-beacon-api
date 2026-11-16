#!/usr/bin/env python3
"""Generate temporary, project-local build secrets; never read production ones."""
from pathlib import Path
import argparse
import os
import secrets
import stat
import sys


def create(directory: Path) -> None:
    directory = directory.expanduser().absolute()
    if ".." in directory.parts or any(path.is_symlink() for path in (directory, *directory.parents)):
        raise ValueError("refusing symlinked credential directory or ancestor")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    created = []
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError("credential directory must be private and owned by this user")
        for name in ("local.env", "db-api-password"):
            try:
                os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise ValueError("refusing to overwrite existing local build credentials")
        values = (("local.env", f"MYSQL_ROOT_PASSWORD={secrets.token_hex(32)}\nOJS_DB_API_USER=ojs_api\n"),
                  ("db-api-password", secrets.token_hex(32) + "\n"))
        for name, value in values:
            output = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                             0o600, dir_fd=descriptor)
            metadata = os.fstat(output)
            created.append((name, metadata.st_dev, metadata.st_ino))
            with os.fdopen(output, "w", encoding="ascii") as destination:
                destination.write(value)
                destination.flush()
                os.fsync(destination.fileno())
        os.fsync(descriptor)
    except BaseException:
        # Roll back only files created by this call, never pre-existing files.
        for name, device, inode in created:
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (metadata.st_dev, metadata.st_ino) == (device, inode):
                os.unlink(name, dir_fd=descriptor)
        raise
    finally:
        os.close(descriptor)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        create(args.directory)
    except (OSError, ValueError):
        print("error: could not create local credentials; use a private owned directory with no existing credential files or symlinks", file=sys.stderr)
        return 1
    print("Created private temporary build credentials; production credentials were not accessed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
