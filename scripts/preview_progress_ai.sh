#!/usr/bin/env bash
# One explicit, capped paid preview. Never send mail or alter recovery.
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
    echo 'Run this preview with sudo.' >&2
    exit 1
fi
# When operational API checks are configured, preview the same evidence as the
# scheduled report. systemd passes the API credential without exposing .secrets.
api_properties=()
if [[ -f /etc/ojs-api/progress-api.env ]]; then
    api_properties+=(
        --property=EnvironmentFile=/etc/ojs-api/progress-api.env
        --property=LoadCredential=api-client.env:/srv/ojs_api/.secrets/api-client.env
    )
fi
# Let systemd allocate a fresh transient unit name. A failed preview's unit can
# remain loaded despite --collect; the report flock still prevents overlap.
exec systemd-run --quiet --collect --wait --pipe \
    --property=Type=oneshot \
    --property=User=ojs --property=Group=ojs \
    --property=SupplementaryGroups=systemd-journal \
    --property=WorkingDirectory=/srv/ojs_api \
    --property=StateDirectory=ojs-api-report --property=StateDirectoryMode=0700 \
    --property=UMask=0077 \
    --property=LoadCredential=openai-api-key:/etc/ojs-api/progress-openai-key \
    --property=Environment=OJS_PROGRESS_LLM_ENABLED=1 \
    --property=NoNewPrivileges=true --property=PrivateTmp=true \
    --property=PrivateDevices=true --property=ProtectSystem=strict \
    --property=ProtectHome=true \
    --property='InaccessiblePaths=-/run/docker.sock -/var/run/docker.sock /srv/ojs_api/.env /srv/ojs_api/.secrets' \
    --property=CapabilityBoundingSet= \
    --property=MemoryMax=96M --property=TasksMax=16 \
    --property=Nice=19 --property=CPUWeight=1 --property=IOWeight=1 \
    --property=TimeoutStartSec=180 \
    "${api_properties[@]}" \
    /usr/bin/python3 -B /srv/ojs_api/src/email_progress.py --preview-llm
