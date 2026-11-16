#!/usr/bin/env bash
# Preparation only: never enables a scheduler or starts an update.
set -Eeuo pipefail
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 -B "$script_dir/install_automatic_updates.py" "$@"
