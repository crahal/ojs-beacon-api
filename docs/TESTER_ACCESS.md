# Individual tester access

Each tester receives a unique username and random API key. All use the same
HTTPS API origin and HTTP Basic authentication; the key is the password, not a
Bearer token. All accounts have the same read-only access. There is no public
signup, user-role system or per-client rate quota.

The original `.secrets/api-client.env` identity remains unchanged for monitoring
and publication. Additional accounts are optional and disabled by default.
Writing invitation files or generating keys does **not** activate a deployed
API. Never advertise a key as ready until an authenticated live check passes.

There are two distinct deployment modes:

- **Interim Caddy gateway:** Caddy checks tester credentials and forwards the
  existing internal identity to an unchanged API. Only the Caddy user list and
  reload control live tester access. This can leave an ongoing data build alone.
- **Native API registry:** an updated API checks a mounted JSON hash registry.
  Initial activation requires coordinated API/configuration maintenance. It is
  not enabled merely by installing the gateway.

Record which mode is active before adding or revoking an account. Both give
testers the same HTTPS URL and Basic-auth interface, but their server-side
registries and revocation procedures are not interchangeable.

## Generate locally, distribute privately

Use the reviewed source on a trusted workstation. Create owner-private folders:

```bash
install -d -m 700 .local/tester-access
install -d -m 700 .local/tester-access/registry .local/tester-access/keys
python3 scripts/manage_api_clients.py init \
  --registry .local/tester-access/registry/clients.json
python3 scripts/manage_api_clients.py issue \
  --registry .local/tester-access/registry/clients.json \
  --username tester01 \
  --output .local/tester-access/keys/tester01.key
```

Repeat with a distinct username/output for each person. Existing keys and
usernames are never silently overwritten. The tool prints only a status, never
the generated key. `issue --help` describes invitation templates and optional UTC
expiry, which is enforced **only in native mode**. Keep the internal monitoring
username reserved; the native API also rejects a registry that collides with
that username.

`clients.json` contains SHA-256 digests of random 256-bit keys, not plaintext keys.
Do not use human-chosen passwords in this registry. Only native mode uses this
JSON file on the server; gateway mode instead requires a separate bcrypt user
list. The raw tester keys, invitations and recipient mapping stay on the
workstation and must not enter Git, Docker, a public source export, or any VM
transfer.
`.gitignore` does not govern Dropbox, other cloud sync, backups or manual copying.

Send one person's invitation directly through an encrypted private channel.
Do not post it to a shared channel or put credentials in an API URL. Never
print keys in a terminal recording, CI output, issue or command-line argument.

## Interim Caddy gateway

Use [the gateway example](../deploy/Caddyfile.gateway.example) with Caddy 2.11.9
or a separately validated compatible version. Retain the deployment's verified
HTTPS identity and certificate configuration. The API must remain reachable
only through the intended proxy/local interface, not a publicly exposed Docker
port that bypasses the gateway.

The `/health` handler remains public and has its incoming `Authorization`
header removed. Caddy normalizes path matches, so even a matching path variant
must reach the API without the injected credential. Catalogue routes require
Caddy Basic authentication. After
successful authentication, the proxy replaces `Authorization` with the original
internal Basic credential. The API therefore sees the internal account, not an
individual tester identity. Do not describe this as native per-client auditing,
expiry, or rate quotas: the gateway provides none of those. Wrong or missing
credentials must produce 401 with `WWW-Authenticate`, not a maintenance 503.
See Caddy's [Basic authentication](https://caddyserver.com/docs/caddyfile/directives/basic_auth)
and [status-specific error handling](https://caddyserver.com/docs/caddyfile/directives/handle_errors)
references.

### Gateway configuration and activation

Prepare the following private server files without sending invitations or
plaintext tester keys to the VM:

- `/etc/caddy/ojs-api-users.caddy`: a header of
  `# internal-monitoring-username: NAME`, followed by one `USERNAME BCRYPT_HASH`
  line per account. Replace the placeholders privately. Include the unchanged
  internal monitoring account as well as every intended tester, with unique
  usernames. Generate tester bcrypt hashes locally from the existing random
  keys; hashes cannot be converted from the native SHA-256 registry. Do not put
  raw keys into hashing command-line arguments or logs.
- `/etc/caddy/ojs-api-upstream.caddy`: exactly one `header_up Authorization`
  directive setting the existing internal Basic credential. Its quoted value
  is `Basic ` followed by the base64-encoded internal `username:key`. Base64 is
  reversible: this file is a secret, not a password-hash file. Build it privately
  from the existing VM credential, without rotating that credential.

Keep the Caddyfile and both imports root-owned, group `caddy`, mode 0640, with
non-writable trusted parent directories. The Caddy service must run as its
dedicated `caddy` account. Its effective autosave directory must be owned by
that account with mode 0700 and the autosave file mode 0600: expanded configuration
includes the upstream secret. Protect private rollback copies equally. Never
print adapted configuration, query the full admin configuration into shared
logs, enable credential logging, or export these files with public source.

The example moves Caddy's admin API to
`unix//run/ojs-caddy-admin/admin.sock`. Configure the service with:

```ini
[Service]
RuntimeDirectory=ojs-caddy-admin
RuntimeDirectoryMode=0700
```

Before a first hot reload, provision that runtime directory with owner `caddy`
and mode 0700; `systemctl daemon-reload` alone does not create it for an already
running service. Use the **currently running** admin address for the first
reload, then the private Unix address for subsequent reloads. Update the service's
`ExecReload` accordingly. Do not expose the admin endpoint over the network or
leave an older unauthenticated admin listener accessible. Preserve the service's
normal boot configuration and verify that it can read the protected imports.

Validate the candidate configuration without exposing expanded secrets, retain
the previous private configuration for rollback, then use a graceful Caddy
reload. No API image/Compose change, API or MySQL restart, builder stop, pipeline
lock bypass, or raw-data reprocessing is needed. The existing updater may
continue; do not alter its environment or source for this gateway activation.
If validation, reload or acceptance fails, restore the previous private Caddy
configuration and reload it through the admin address that is actually active.

Check HTTPS health; anonymous and wrong-key rejection; every tester; the original
internal account; and a deliberately revoked test account. Confirm the 401
challenge survives, `/health/` does not bypass authentication, normal API queries
still work, and the API/MySQL container identities, catalogue and advancing build
remain unchanged. `/health` alone never proves tester authentication works.

### Revoke a gateway account

From the reviewed server checkout, run as root:

```bash
sudo python3 scripts/manage_gateway_clients.py --username tester01
```

The gateway tool removes only the named tester from the private Caddy user list,
validates the configuration, and gracefully reloads it, attempting rollback on
failure. If it reports unconfirmed rollback, inspect and restore the gateway
state before retrying or claiming revocation succeeded.
It refuses the reserved internal monitoring identity. Verify a request with the
revoked credentials returns 401 and another active tester still works. Requests
already authorized may finish. Removal is effective only after the successful
reload; editing a file alone does not revoke the running configuration.

`manage_api_clients.py revoke` and JSON `expires_at` do **not** revoke or expire a
gateway account. This mode has no automatic expiry or per-tester quota. Treat the
live Caddy list as authoritative, record revocations in the private inventory,
and reconcile it before restoring old configuration or migrating to native mode.

## Native API registry: first activation

Existing deployments need the updated API image, reviewed Compose configuration,
the dedicated registry directory, and explicit configuration. This is not just
an image-only rollout: the existing `update_api_image.py` helper must not be used
to bypass its configuration/credential maintenance restrictions.

1. Keep the currently serving API and internal credentials unchanged while a
   data build is advancing. Wait for build/publication to finish, pause new
   automatic work, and use the existing workflow locks during maintenance.
   See [operations](OPERATIONS.md#application-and-dependency-upgrades).
2. Preserve an approved private rollback copy of the existing API image and
   configuration. Do not change the MySQL configuration or restart the database.
3. On the VM, create `.secrets/api-clients` owned by the deployment account,
   mode 0700. Copy **only** the reviewed hash registry there as `clients.json`,
   owned by that account and mode 0600. Do not sync its source parent directory.
4. Set `OJS_API_CLIENTS_HOST_DIR=./.secrets/api-clients` and
   `OJS_API_CLIENTS_FILE=/run/secrets/ojs-api-clients/clients.json` in the private
   deployment environment. Leave `OJS_API_CREDENTIALS_HOST_FILE` unchanged.
   The dedicated directory is mounted read-only; mounting an individual file
   would hide atomic replacements from an already running container.
5. Recreate **only** the API using the reviewed image/configuration, with no
   database dependency restart. If any acceptance check fails, restore the
   prior image/configuration and verify the original internal account.
6. Verify HTTPS health; unauthenticated rejection; the original internal
   identity; every new identity; a wrong key; and individually revoked/expired
   test accounts. Check metadata, snapshot history and one article, and verify
   the MySQL container identity/start time and catalogue are unchanged.
7. Mark the invitations ready only after those checks pass, then resume the
   approved updater schedule. The authentication change requires no rewrangling.

The initial mount directory must be provisioned with the stated ownership before
enabling accounts. Docker may create a missing bind directory as root; do not
work around this by broadening file permissions or mounting all of `.secrets`.

If migrating from the gateway, first reconcile every gateway revocation into the
native registry. Coordinate the new API configuration with removal of **both**
the proxy's tester `basic_auth` gate and the injected upstream `Authorization`
override as one controlled cutover. The original client header must reach the
native API unchanged; leaving the override in place makes all accepted users
appear to be the internal account and bypasses native tester revocation. Preserve
HTTPS, public health handling and the original internal account, test revocation
end-to-end, and roll back the API/proxy pair together if any check fails.

## Native mode: revoke or expire a client

After activation, the API reloads the small registry for each tester request.
On the server, run as the deployment account:

```bash
python3 scripts/manage_api_clients.py revoke \
  --registry .secrets/api-clients/clients.json --username tester01
```

This atomically disables only that account. No API restart, key rotation for
other clients, or database changes are required. Requests already authorized
may finish. Optional expiry is enforced in UTC. Protect against restoring an
old registry accidentally re-enabling revoked accounts: treat the active server
copy as authoritative and reconcile local inventories before importing changes.

An absent, malformed or insecure configured registry prevents API startup.
If it becomes unavailable after startup, tester authentication fails closed;
the original internal monitoring identity can continue to operate. `/health`
alone therefore does not prove tester access works. Test an active tester
account separately after registry maintenance.
