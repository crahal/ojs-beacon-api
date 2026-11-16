# Security and privacy

## Reporting a vulnerability

Do not post credentials, raw dumps, article payloads, private IPs, `.env` files or
unredacted logs in public issues. Use GitHub's **Security → Report a vulnerability**
when private reporting is enabled for the repository. If it is unavailable,
contact a maintainer through an already established private channel; do not
publish an exploit or secret while arranging contact. Include affected code
version, impact, a minimal synthetic reproduction and proposed mitigation.

There is no guaranteed response SLA or independently certified security audit.
Maintainers should enable private vulnerability reporting, secret scanning where
available and branch protections before inviting public contributions. Review
dependency/security updates before production deployment; pinned versions are
reproducible, not perpetually safe.

## Deployment boundaries

- Basic authentication provides no encryption itself: use trusted HTTPS for all
  non-loopback access. Do not bypass certificate verification. A public API's
  public server IP/origin is necessarily discoverable; this is distinct from
  an administrator's private workstation identity.
- All catalogue/OAI routes require API credentials; only `/health` is public.
  Interactive documentation/schema endpoints are disabled. Use a monitored
  reverse proxy for TLS and additional rate/abuse controls; application
  concurrency/page limits are not a complete denial-of-service defence.
- Never expose MySQL or the API's internal port directly. Compose binds the API
  to loopback and does not publish a MySQL port. Restrict SSH to explicit trusted
  administrator CIDRs and verify SSH host fingerprints independently.
- The serving API's MySQL account is SELECT-only. Build/publish processes require
  stronger privileges and must not be available as web endpoints. Treat the
  `ojs` deployment account and Docker access as privileged: Docker-group access
  can control the host. Untrusted users must not share that account.
- Keep `.env` and credential files mode 0600, secret directories mode 0700 and
  backups encrypted/access-controlled. The current MySQL Compose configuration
  retains its root password in the container environment;
  privileged local/container administrators can inspect it. This deployment is
  not designed as a hostile multi-tenant host.
- MySQL's passwordless container ping is only a liveness check; authenticated API
  and publication checks establish usable credentials/data. The internal client
  can negotiate TLS but does not currently verify MySQL server identity. Keep
  that database on the private single-host network; a remote database needs a
  separately designed certificate-verified connection configuration.
- Do not mix the API client key, API database password, database root password,
  upstream credential, SSH key, email app password or optional OpenAI API key.
  Use separately generated values and rotate the affected credential if exposed.
- Configuration files consumed as shell environment are trusted executable
  configuration. Keep them administrator-controlled; never source a contributed
  or downloaded `.env` blindly.

## Logs and optional reporting

API request access logs default to **off**. `OJS_API_ACCESS_LOG=1` explicitly
enables client IP and full path/query logging. Reverse proxies, SSH, Docker and
the operating system can independently log addresses and hostnames. Keep all
operational logs private, bounded and reviewed before sharing. Disabling API
access logs is not anonymization of every system around the API.

Health-check errors log only exception type, not raw exception messages.
Downloader/scraper errors avoid printing URL credentials. These safeguards do
not prove arbitrary upstream records or third-party errors cannot contain
personal information. Never use secrets as URL query parameters or enable shell
tracing around credential handling.

Email reporting is opt-in and sends operational observations to configured
recipients. Optional AI interpretation sends a restricted observation to an
external model service and requires a separately configured API credential.
Review its payload, recipients and budget before enabling it; never include raw
research data, credentials or unrestricted journal output in prompts. Neither
email nor AI reporting is required to serve the API. See [operations](docs/OPERATIONS.md).

## Repository sharing

`make check-repository` checks candidate files without reading private/data paths;
`make check-history` also checks reachable Git history and author metadata.
Rules reject non-example IPs/emails, machine paths, selected infrastructure IDs,
credential patterns, binary/archive/data artifacts, symlinks and oversized files.
This bounded heuristic cannot identify every secret, arbitrary personal prose,
deleted/unreachable object, external fork, GitHub issue or CI artifact.

Deleting a sensitive file from the latest commit does not remove previous
versions. Use [the clean-source sharing workflow](docs/SHARING.md) if history
contains private details. Never make an existing repository public solely because
the current-tree check passes. Review the exported source manually as well.

If a real credential was exposed, revoke/rotate it first. History cleanup needs
coordination and cannot recall other people's copies or forks. Follow
[GitHub's sensitive-data guidance](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository).
