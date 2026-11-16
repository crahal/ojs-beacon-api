#!/usr/bin/env bash
# One factual, read-only API/report preview. No mail, paid AI, or state writes.
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
    echo 'Run this preview with sudo.' >&2
    exit 1
fi
if [[ ! -f /etc/ojs-api/progress-api.env || -L /etc/ojs-api/progress-api.env ]]; then
    echo 'Configure the live API origin with configure_progress_api.py first.' >&2
    exit 1
fi
# No StateDirectory is requested: the preview must not create or update state.
# The API credential is copied by systemd; the source secrets remain inaccessible.
exec systemd-run --quiet --collect --wait --pipe \
    --property=Type=oneshot \
    --property=User=ojs --property=Group=ojs \
    --property=SupplementaryGroups=systemd-journal \
    --property=WorkingDirectory=/srv/ojs_api \
    --property=UMask=0077 \
    --property=EnvironmentFile=/etc/ojs-api/progress-api.env \
    --property=LoadCredential=api-client.env:/srv/ojs_api/.secrets/api-client.env \
    --property=Environment=OJS_PROGRESS_LLM_ENABLED=0 \
    --property=NoNewPrivileges=true --property=PrivateTmp=true \
    --property=PrivateDevices=true --property=ProtectSystem=strict \
    --property=ProtectHome=true \
    --property='InaccessiblePaths=-/run/docker.sock -/var/run/docker.sock /srv/ojs_api/.env /srv/ojs_api/.secrets' \
    --property=ProtectKernelTunables=true --property=ProtectKernelModules=true \
    --property=ProtectControlGroups=true --property=LockPersonality=true \
    --property=RestrictSUIDSGID=true \
    --property='RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6' \
    --property=CapabilityBoundingSet= \
    --property=MemoryMax=96M --property=TasksMax=16 \
    --property=Nice=19 --property=CPUWeight=1 --property=IOWeight=1 \
    --property=TimeoutStartSec=180 \
    /usr/bin/python3 -B /srv/ojs_api/src/email_progress.py --dry-run
