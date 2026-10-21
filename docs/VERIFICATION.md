# Local verification — 2026-10-11

The local Docker deployment was exercised with real PostgreSQL, S3 storage,
Dex OIDC and ClamAV. The browser uses the same authenticated API and direct
storage transfers as the HTTP integration tests.

## Automated checks

**43 tests passed across the module suites.**

| Suite | Passed | Evidence covered |
|---|---:|---|
| API integration | 8 | All 29 documented operations; response schemas, media types and formats; authorization, versions, sharing, trash, limits and logout |
| Security invariants | 4 | Real identity refresh and failure, persistent revocation, append-only audit, encrypted share replay |
| Analytics and instrumentation | 3 | Access boundaries, mature cohort arithmetic, deduplication and bounded metric labels |
| Chromium journeys | 5 | Real sign-in, multipart transfers, downloads, version restoration, sharing, admin views, keyboard controls and mobile layout |
| Storage unit checks | 10 | Bounded scanning, explicit clean results, checksum rejection, multipart manifests and signed byte limits |
| Worker fault integration | 11 | Scanner outage and admin retry, lease fencing, interrupted completion/purge, quota races, expiry and the 20-session limit |
| Backup integrity | 2 | Streaming archive round-trip and rejection of altered ciphertext before parsing |

The fault suite passed in 378.37 seconds. It includes waiting through an actual
60-second download capability expiry. Test fixtures restore policies and worker
availability; the capacity test expires and cleans only its own sessions.

The final API suite passed in 15.18 seconds. Its coverage artifact contains all
29 operations from `FileFlow_OpenAPI.yaml`, with none missing. Search isolation
checks allow existing grants to other demo files while asserting that the new,
unshared file remains invisible.

## File-size and memory boundary

An exact 2 GiB transfer exposed ClamAV's hard ceiling: the upload completed, but
the antivirus rejected it and FileFlow kept it quarantined. The API, browser
and contract now cap new uploads at **2,147,483,645 bytes (2 GiB minus 3 bytes)**,
matching the [pinned engine's limit](https://github.com/Cisco-Talos/clamav/blob/clamav-1.5.4/libclamav/others.c#L628-L642).

The supported maximum passed the complete flow:

- 256 multipart parts; upload took 70.25 seconds, including rate-limit waits.
- Real antivirus scan returned clean on its first attempt.
- Streamed download matched both the byte count and SHA-256.
- Trash, retention expiry and physical purge completed through the real worker.
- Total test duration, including scan, download and cleanup: 236.71 seconds.

Sampled peak aggregate container memory was **1.85 GiB**, including monitoring.
The worker peaked at 76 MiB and antivirus at 1.26 GiB. The host test client peaked
at 323 MiB. Samples are observations, not a proof of every instantaneous peak;
Docker memory caps remain enforced.

## Performance, monitoring and recovery

A burst of 50 concurrent metadata requests through the proxy, using two
accounts, returned 50 successful responses. p95 was **406.56 ms**, mean 293.07 ms.
This is one local burst, not a sustained 50-person production load test.

Prometheus reported all four scrape targets up. `promtool check rules` accepted
all ten alert rules. No external notification receiver is configured.

The installed `fileflow-backup.timer` schedules recovery sets every 12 hours.
Its service completed a real encrypted backup successfully, with a 135 MiB
memory peak. An isolated restore recovered **47 objects and 36 active files**,
verified checksums and database references, and removed its temporary database
and bucket. The drill took **4.21 seconds**. Concurrent backup creation was also
confirmed to fail before entering a maintenance window.

Generated credentials were absent from tracked files and the inspected recent
service logs. Private configuration, credentials, backup keys and test reports
are excluded from Git.

## Reproduction and scope

Commands and account roles are in [Local development](LOCAL_DEVELOPMENT.md).
Generated evidence on this machine includes:

- `.local/api-results.xml`, `.local/unit-results.xml`, `.local/api-coverage.json`
- `.local/large-upload-supported-boundary.json` and `.local/large-upload-2gib-rejected.json`
- `.local/metadata-load.json`, `.local/backup-status.json`, `.local/restore-drill.json`
- `.local/browser-desktop.png`, `.local/browser-mobile.png`, `.local/secret-check.json`

The local profile uses loopback HTTP and five demo identities. Live volumes
are not configured with disk encryption or KMS; recovery archives are encrypted.
The full Firefox/Edge version matrix, 200-employee workload, monthly uptime and
off-host disaster recovery targets remain outside these local measurements.
