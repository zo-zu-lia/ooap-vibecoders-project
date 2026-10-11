# Running FileFlow locally

FileFlow now has a working local implementation of the documented file manager.
The application is Python/FastAPI with PostgreSQL, a separate worker, private
S3 storage, real ClamAV scanning, Dex OIDC, and a vanilla JavaScript interface.
There is no frontend compilation or external CDN dependency.

## Start and sign in

Prerequisites: Docker Engine with Compose v2, Python 3.13 or newer, and Python
venv support. Docker was installed on this machine as part of implementation.

```sh
./scripts/start.sh
```

Open **http://localhost:8080**. Read `.local/credentials.txt` for the generated
password. The local identity provider has these accounts:

| Account | Access |
|---|---|
| `owner@fileflow.local` | Owner of Design team; can manage access and sharing |
| `editor@fileflow.local` | Can upload and edit Design team files |
| `viewer@fileflow.local` | Can read Design team files |
| `security@fileflow.local` | Policies, legal holds, audit, analytics and failed jobs; no implicit file access |
| `finance@fileflow.local` | Owner of Finance; useful for testing isolation and addressed sharing |

Passwords, OIDC client credentials, application encryption keys, and database
credentials are generated locally and excluded from Git. Re-running setup
preserves them. Demo memberships are seeded once; restarting does not restore
revoked grants. Dex uses real Authorization Code + PKCE; there is no application
login bypass or password endpoint.

For a remote development VM, forward **both** ports 8080 and 9000 to your own
machine. The browser must reach the storage endpoint directly:

```sh
ssh -L 8080:127.0.0.1:8080 -L 9000:127.0.0.1:9000 user@your-development-host
```

This local profile deliberately uses loopback HTTP and an HttpOnly,
SameSite=Lax session cookie. A non-loopback deployment needs HTTPS for the app,
IdP and storage, `COOKIE_SECURE=true`, consistent public origins, and a real
corporate IdP. Do not expose the local demo credentials or HTTP ports publicly.

Live database and object volumes use the host filesystem without application
managed disk encryption or MinIO KMS. Recovery archives are encrypted separately.
The SRS requirement for encrypted live storage needs host disk encryption or a
storage KMS when preparing a production deployment.

## Everyday commands

```sh
./scripts/compose.sh ps
./scripts/compose.sh logs --tail=100 api worker
./scripts/compose.sh stop
./scripts/compose.sh up -d --wait

# Rebuild after source changes; migrations run before API/worker startup.
./scripts/compose.sh build api
./scripts/compose.sh up -d --wait
```

Named volumes preserve the database, objects and ClamAV signatures. Dex state
is in `.local/dex-data`. Avoid `down -v` when keeping data. Nginx refreshes
Docker DNS so recreating the API does not leave it pointing at an old address.

Service images and Python dependencies are pinned. Storage is built from
official MinIO source at commit
`9e49d5e7a648f00e26f2246f4dc28e6b07f8c84a`. The initial storage build can take
several minutes. Go builds one package at a time with a 2 GiB memory target.
ClamAV's first start downloads signatures and waits for the scanner to be ready.

The API and worker each have a 384 MiB cap; storage 512 MiB; PostgreSQL 384 MiB;
ClamAV 2.25 GiB; Dex 128 MiB; proxy 64 MiB. Pools are bounded (API eight database
connections, worker four). The worker processes one job at a time and streams
file bytes in small buffers. Browser hashing runs incrementally in a worker,
and at most four 8 MiB parts upload concurrently.

### Startup troubleshooting

If `migrate` exits with `password authentication failed`, inspect its logs:

```sh
./scripts/compose.sh logs --tail=50 migrate
```

`POSTGRES_PASSWORD` and the initialization scripts only configure a new
database volume. Recreating containers with changed credentials does not update
the passwords already stored in PostgreSQL. When the intended local passwords
are those in `.env`, repair the roles without deleting the volume:

```sh
./scripts/repair-db-credentials.sh
./scripts/compose.sh up -d --wait
```

The repair uses the local PostgreSQL administrator socket and changes only the
two database role passwords, creating the application role if it is missing.
It preserves tables and data. Do not use `down -v` to resolve this error.

An orphan warning naming Prometheus, Alertmanager, Blackbox or Node Exporter
means the monitoring overlay was omitted from the current command. Keep using
both files when managing a deployment with monitoring enabled:

```sh
./scripts/compose.sh -f compose.yaml -f compose.monitoring.yaml up -d --wait
```

That warning does not cause migration failure. `--remove-orphans` would remove
those monitoring services.

## Implemented behavior

- All 29 documented REST operations, plus authenticated directory, direct-grant
  discovery, administrator job management and analytics helpers.
- Server-side OIDC sessions, CSRF protection, effective workspace/group/file
  permissions, private filename search, and append-only audit records. The
  application database role cannot rewrite or truncate audit history.
- Resumable multipart sessions, SHA-256 verification and actual ClamAV scanning.
  Pending or rejected bytes are never downloadable. A previous clean version
  remains available while a replacement is checked.
  The maximum accepted file is **2,147,483,645 bytes (2 GiB minus 3 bytes)**.
  This is ClamAV's hard whole-file scanning ceiling; the API and browser reject
  larger files before transfer. The original SRS's exact 2 GiB target differs
  by three bytes. See the [upstream engine constraint](https://github.com/Cisco-Talos/clamav/blob/clamav-1.5.4/libclamav/others.c#L628-L642).
- Immutable versions, optimistic version conflicts, idempotent completion,
  atomic quota reservation, and exact signed part lengths.
- Recipient-bound internal share links; only hashes are stored directly, and
  secret-bearing idempotency replies are encrypted. Signed downloads last at
  most 60 seconds; already-started transfers can continue beyond expiry.
- Trash, restoration, retention, legal holds, confirmed storage deletion,
  durable PostgreSQL jobs, fenced leases, retries, and crash reconciliation.
- Twelve analytics metrics and two funnels with explicit provisional or
  unavailable values. Counts refer to issued links, not completed downloads.
  Owner analytics suppress small cohorts; administrators can inspect diagnostic
  counts. No file names, search text, or secret URLs enter analytics events.

## Backups and recovery checks

```sh
.venv/bin/python scripts/backup.py create
.venv/bin/python scripts/backup.py verify backups/ARCHIVE.ffbackup
.venv/bin/python scripts/backup.py drill backups/ARCHIVE.ffbackup
```

Creation briefly stops the API and worker to take a consistent recovery set,
then restarts them and waits for healthy services. Scheduled and manual backups
cannot overlap. The AES-GCM authenticated archive contains a PostgreSQL
dump, complete storage objects including metadata, and a checksum manifest.
Memory use is bounded; encryption does not load all objects into RAM. Successful
creation retains 30 days of archives and updates the backup-age metric.

**Preserve `.local/backup.key` separately from the archives.** Without the
original key the backups cannot be decrypted. Copy encrypted recovery sets and
the key to separate protected locations for host-loss recovery; local backups
alone do not survive loss of this machine.
Keep `.env` and `.local/dex.yaml` in a protected configuration backup as well:
the recovery archive covers database and object data, not application keys or
identity-provider configuration.

The drill restores into a fresh temporary database and bucket, revokes restored
sessions, validates every object and clean-version checksum, then deletes only
its temporary resources. It never overwrites the live database. Promoting an
old snapshot to a live environment also requires reconciling subsequent access
revocations and deletions; the drill is not an unattended production restore.

To install the host's 12-hour backup timer:

```sh
.venv/bin/python scripts/install-backup-timer.py
systemctl status fileflow-backup.timer
sudo journalctl -u fileflow-backup.service
```

The timer runs as the checkout owner and uses Docker through `compose.sh`.
Disable it with `sudo systemctl disable --now fileflow-backup.timer` before
removing or moving the checkout.

## Monitoring and failed jobs

```sh
./scripts/compose.sh -f compose.yaml -f compose.monitoring.yaml up -d
```

Prometheus is at **http://localhost:9090** and Alertmanager at
**http://localhost:9093**. Metrics ports and PostgreSQL stay private. The public
proxy blocks `/metrics`. Four targets collect application, host and edge health
metrics. Ten alert rules cover availability, latency/errors, dependencies, jobs,
resource pressure, disk, database, backups, TLS and missing scrapes. The TLS rule
is inactive for the local HTTP profile. Alerts are retained in the local
dashboard; no external messages are sent.

These monitors share the host. Full host failure still requires an independent
off-host probe and notification receiver before a production deployment.

Sign in as `security@fileflow.local` and open **Failed jobs** to inspect a failed
job. Repair the dependency first, then use Retry. Retrying creates a new fenced
lease; it does not mark unchecked content clean. Health checks cover the API's
dependencies and the worker's most recent successful database heartbeat.

## Verification

```sh
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/playwright install --with-deps chromium
.venv/bin/pytest -q

# Explicit capacity checks, outside the default test suite:
.venv/bin/python tests/metadata_load.py
.venv/bin/python tests/large_upload.py
```

Tests use real OIDC, PostgreSQL, S3 and ClamAV. Browser tests cover desktop and
mobile flows. Worker fault tests temporarily stop the worker and restore it in
fixture teardown; run them when no one is using the local demo. Test files and
audit entries are synthetic. The app's actual rate limiter remains enabled.

The test helpers validate responses against `docs/FileFlow_OpenAPI.yaml` and
record exercised operations in `.local/api-coverage.json`. Screenshots, backup
reports and local measurement results are kept in `.local` or `test-results`,
outside Git. The SRS availability, disaster recovery and full-capacity figures
are targets, not production guarantees proven by this local demonstration.

The maximum-size check streams real bytes through storage, checksum verification,
ClamAV and download, then removes its own fixture. It needs several minutes and
at least 2 GiB of free quota. See [the recorded verification](VERIFICATION.md)
for the results obtained on this machine.
