"""Durable PostgreSQL jobs with leases, fencing, and bounded retries.

One worker process performs one stream at a time, keeping memory independent of
file size. Multiple processes safely compete using SKIP LOCKED.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from contextlib import AbstractContextManager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from . import storage
from .db import connection
from .security import audit
from .uploads import enqueue, finish_upload, utcnow

log = logging.getLogger("fileflow.worker")
STOP = threading.Event()
LEASE_SECONDS = 120
MAX_ATTEMPTS = 3
HEARTBEAT_FILE = Path(os.getenv("WORKER_HEARTBEAT_FILE", "/tmp/fileflow-worker-heartbeat"))
_last_health_stamp = 0.0


def health_heartbeat():
    """Called only after a committed database check or successful lease renew."""
    global _last_health_stamp
    instant = time.monotonic()
    if instant - _last_health_stamp >= 10:
        HEARTBEAT_FILE.touch(mode=0o600, exist_ok=True)
        _last_health_stamp = instant


class LeaseLost(RuntimeError):
    pass


def claim_job():
    with connection() as conn:
        # A crash during the final attempt must become visible as FAILED rather
        # than leave a RUNNING row that nobody can ever claim again.
        conn.execute(
            """UPDATE jobs SET state='FAILED',error_code='LEASE_EXPIRED',updated_at=now()
               WHERE state='RUNNING' AND lease_until<now() AND attempts>=%s""", (MAX_ATTEMPTS,)
        )
        job = conn.execute(
            """SELECT * FROM jobs WHERE attempts<%s AND
               ((state='QUEUED' AND next_run_at<=now()) OR (state='RUNNING' AND lease_until<now()))
               ORDER BY next_run_at,created_at FOR UPDATE SKIP LOCKED LIMIT 1""", (MAX_ATTEMPTS,)
        ).fetchone()
        if not job:
            return None
        return conn.execute(
            """UPDATE jobs SET state='RUNNING',attempts=attempts+1,lease_token=%s,
               lease_until=now()+make_interval(secs => %s),updated_at=now()
               WHERE id=%s RETURNING *""", (uuid4(), LEASE_SECONDS, job["id"]),
        ).fetchone()


def owned(conn, job):
    current = conn.execute(
        """SELECT * FROM jobs WHERE id=%s AND state='RUNNING' AND lease_token=%s
           AND lease_until>now() FOR UPDATE""", (job["id"], job["lease_token"]),
    ).fetchone()
    if not current:
        raise LeaseLost("Job lease no longer belongs to this worker")
    return current


def done(conn, job):
    owned(conn, job)
    conn.execute(
        "UPDATE jobs SET state='DONE',lease_until=NULL,error_code=NULL,updated_at=now() WHERE id=%s",
        (job["id"],),
    )


class Lease(AbstractContextManager):
    def __init__(self, job):
        self.job = job
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(target=self._renew, daemon=True)

    def _renew(self):
        while not self.stop.wait(20):
            try:
                with connection() as conn:
                    row = conn.execute(
                        """UPDATE jobs SET lease_until=now()+make_interval(secs => %s),updated_at=now()
                           WHERE id=%s AND state='RUNNING' AND lease_token=%s AND lease_until>now()
                           RETURNING id""", (LEASE_SECONDS, self.job["id"], self.job["lease_token"]),
                    ).fetchone()
                if not row:
                    self.lost.set()
                    return
                health_heartbeat()
            except Exception:
                # Losing contact with the lease store is not permission to
                # publish a result. A future owner safely retries the stream.
                self.lost.set()
                return

    def check(self):
        if self.lost.is_set():
            raise LeaseLost("Lease renewal failed")
        if STOP.is_set():
            raise LeaseLost("Worker is shutting down")

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=6)


def scan(job, lease):
    with connection() as conn:
        upload = conn.execute("SELECT * FROM upload_sessions WHERE id=%s", (job["upload_id"],)).fetchone()
        if not upload:
            done(conn, job)
            return
        version = conn.execute(
            """SELECT v.scan_status,f.purge_state FROM file_versions v JOIN files f ON f.id=v.file_id
               WHERE v.upload_id=%s""", (upload["id"],)
        ).fetchone()
        if not version or version["scan_status"] != "PENDING_SCAN" or version["purge_state"] in {"PURGING", "PURGED"}:
            done(conn, job)
            return
    started = time.monotonic()
    outcome = "technical_failure"
    try:
        rejection = storage.verify_object(upload, lease.check)
        outcome = "rejected" if rejection else "clean"
    finally:
        try:
            from .observability import observe_scan
            observe_scan(time.monotonic() - started, outcome, upload["size_bytes"])
        except Exception:
            # Metrics must never turn a technical scanner failure into CLEAN,
            # or prevent a valid fenced result from being committed.
            log.warning("scan_observation_unavailable job_id=%s", job["id"])
    lease.check()
    with connection() as conn:
        conn.execute("SELECT id FROM workspaces WHERE id=%s FOR UPDATE", (upload["workspace_id"],))
        file = conn.execute("SELECT * FROM files WHERE id=%s FOR UPDATE", (upload["file_id"],)).fetchone()
        owned(conn, job)
        if file["purge_state"] not in {"PURGING", "PURGED"}:
            changed = conn.execute(
                "UPDATE file_versions SET scan_status=%s WHERE upload_id=%s AND scan_status='PENDING_SCAN' RETURNING file_id",
                ("REJECTED" if rejection else "CLEAN", upload["id"]),
            ).fetchone()
            if changed:
                conn.execute(
                    "UPDATE upload_sessions SET state=%s,error_code=%s WHERE id=%s",
                    ("REJECTED" if rejection else "READY", rejection, upload["id"]),
                )
                audit(conn, None, "SCAN_REJECTED" if rejection else "SCAN_CLEAN", upload["file_id"], uuid4(),
                      result="REJECTED" if rejection else "SUCCESS", workspace_id=upload["workspace_id"],
                      effect_key=f"scan:{upload['id']}")
        done(conn, job)


def purge(job, lease):
    with connection() as conn:
        file = conn.execute("SELECT * FROM files WHERE id=%s", (job["target_id"],)).fetchone()
        if not file:
            done(conn, job)
            return
        conn.execute("SELECT id FROM workspaces WHERE id=%s FOR UPDATE", (file["workspace_id"],))
        file = conn.execute("SELECT * FROM files WHERE id=%s FOR UPDATE", (job["target_id"],)).fetchone()
        owned(conn, job)
        if file["purge_state"] in {"ACTIVE", "PURGED"}:
            done(conn, job)
            return
        if file["purge_state"] == "TRASHED":
            if file["legal_hold"] or not file["purge_at"] or file["purge_at"] > utcnow():
                # A legal hold is not a technical failure or an exhausted retry.
                next_run = max(file["purge_at"] or utcnow(), utcnow() + timedelta(minutes=5))
                conn.execute(
                    """UPDATE jobs SET state='QUEUED',attempts=greatest(attempts-1,0),lease_until=NULL,
                       next_run_at=%s,updated_at=now() WHERE id=%s""", (next_run, job["id"]),
                )
                return
            conn.execute(
                """UPDATE files SET purge_state='PURGING',updated_at=now()
                   WHERE id=%s AND purge_state='TRASHED' AND NOT legal_hold AND purge_at<=now()""", (file["id"],)
            )
        objects = conn.execute(
            """SELECT DISTINCT o.* FROM storage_objects o JOIN file_versions v ON v.object_id=o.id
               WHERE v.file_id=%s AND o.state='AVAILABLE' AND NOT EXISTS
               (SELECT 1 FROM file_versions other JOIN files f ON f.id=other.file_id
                WHERE other.object_id=o.id AND other.file_id<>%s AND f.purge_state<>'PURGED')""",
            (file["id"], file["id"]),
        ).fetchall()
    # No database transaction spans storage I/O. PURGING is irreversible and
    # blocks both recover and a new legal hold before any bytes are removed.
    for obj in objects:
        lease.check()
        storage.delete_confirmed(obj["object_key"])
    lease.check()
    with connection() as conn:
        conn.execute("SELECT id FROM workspaces WHERE id=%s FOR UPDATE", (file["workspace_id"],))
        current = conn.execute("SELECT * FROM files WHERE id=%s FOR UPDATE", (file["id"],)).fetchone()
        owned(conn, job)
        if current["purge_state"] == "PURGING":
            freed = 0
            for obj in objects:
                changed = conn.execute(
                    "UPDATE storage_objects SET state='DELETED' WHERE id=%s AND state='AVAILABLE' RETURNING size_bytes",
                    (obj["id"],),
                ).fetchone()
                if changed:
                    freed += changed["size_bytes"]
            conn.execute("UPDATE workspaces SET used_bytes=used_bytes-%s WHERE id=%s", (freed, file["workspace_id"]))
            conn.execute("UPDATE files SET purge_state='PURGED',updated_at=now() WHERE id=%s", (file["id"],))
            audit(conn, None, "FILE_PURGED", file["id"], uuid4(), workspace_id=file["workspace_id"],
                  effect_key=f"purge:{file['id']}")
        done(conn, job)


def cleanup(job, lease):
    with connection() as conn:
        upload = conn.execute("SELECT * FROM upload_sessions WHERE id=%s", (job["upload_id"],)).fetchone()
        if not upload:
            done(conn, job)
            return
        conn.execute("SELECT id FROM workspaces WHERE id=%s FOR UPDATE", (upload["workspace_id"],))
        upload = conn.execute("SELECT * FROM upload_sessions WHERE id=%s FOR UPDATE", (job["upload_id"],)).fetchone()
        owned(conn, job)
        if upload["version_no"] or upload["state"] in {"COMPLETING", "PROCESSING", "READY"}:
            done(conn, job)
            return
        if upload["state"] in {"INITIATING", "INITIATED"}:
            if upload["expires_at"] > utcnow():
                done(conn, job)
                return
            conn.execute("UPDATE upload_sessions SET state='EXPIRED',error_code='UPLOAD_EXPIRED' WHERE id=%s", (upload["id"],))
        elif upload["state"] not in {"CONFLICT", "EXPIRED", "REJECTED"}:
            raise RuntimeError("Unrecognized upload state; refusing orphan cleanup")
        # Defense in depth: a referenced object is never an orphan, irrespective
        # of a bad or stale session state.
        reference = conn.execute("SELECT id FROM storage_objects WHERE object_key=%s", (upload["object_key"],)).fetchone()
        if reference:
            raise RuntimeError("Refusing cleanup of a referenced storage object")
    lease.check()
    storage.abort_and_delete(upload)
    lease.check()
    with connection() as conn:
        conn.execute("SELECT id FROM workspaces WHERE id=%s FOR UPDATE", (upload["workspace_id"],))
        current = conn.execute("SELECT * FROM upload_sessions WHERE id=%s FOR UPDATE", (upload["id"],)).fetchone()
        owned(conn, job)
        if current["state"] not in {"CONFLICT", "EXPIRED", "REJECTED"} or current["version_no"]:
            raise RuntimeError("Upload state changed during cleanup")
        if current["reserved_bytes"]:
            conn.execute(
                "UPDATE workspaces SET reserved_bytes=reserved_bytes-%s WHERE id=%s",
                (current["reserved_bytes"], upload["workspace_id"]),
            )
            conn.execute("UPDATE upload_sessions SET reserved_bytes=0 WHERE id=%s", (upload["id"],))
        audit(conn, None, "UPLOAD_CLEANED", upload["id"], uuid4(), workspace_id=upload["workspace_id"],
              effect_key=f"cleanup:{upload['id']}")
        done(conn, job)


def reconcile():
    """Queue work from authoritative state, never delete objects merely by age."""
    with connection() as conn:
        for upload in conn.execute(
            """SELECT id FROM upload_sessions WHERE
               (state IN ('INITIATING','INITIATED') AND expires_at<=now()) OR
               (state IN ('CONFLICT','EXPIRED') AND reserved_bytes>0) LIMIT 100"""
        ).fetchall():
            enqueue(conn, "CLEANUP", upload["id"], upload["id"])
        for upload in conn.execute(
            "SELECT id FROM upload_sessions WHERE state='COMPLETING' AND complete_started_at<now()-interval '30 seconds' LIMIT 100"
        ).fetchall():
            enqueue(conn, "COMPLETE", upload["id"], upload["id"])
        for file in conn.execute(
            """SELECT id,deleted_at FROM files WHERE
               (purge_state='TRASHED' AND purge_at<=now() AND NOT legal_hold) OR purge_state='PURGING' LIMIT 100"""
        ).fetchall():
            generation = int(file["deleted_at"].timestamp() * 1000000) if file["deleted_at"] else 0
            enqueue(conn, "PURGE", file["id"], generation=generation)


def fail(job, exc):
    code = "SCANNER_UNAVAILABLE" if isinstance(exc, storage.ScannerUnavailable) else "JOB_FAILED"
    with connection() as conn:
        current = owned(conn, job)
        state = "FAILED" if current["attempts"] >= MAX_ATTEMPTS else "QUEUED"
        conn.execute(
            """UPDATE jobs SET state=%s,error_code=%s,lease_until=NULL,
               next_run_at=now()+make_interval(secs => %s),updated_at=now() WHERE id=%s""",
            (state, code, 10 * 2 ** (current["attempts"] - 1), job["id"]),
        )
        if job["kind"] == "SCAN":
            conn.execute("UPDATE upload_sessions SET error_code=%s WHERE id=%s AND state='PROCESSING'", (code, job["upload_id"]))
        if state == "FAILED":
            audit(conn, None, "JOB_FAILED", job["target_id"], uuid4(), result="FAILED", effect_key=f"job-failed:{job['id']}:{job['lease_token']}")
    log.error("job_failed job_id=%s kind=%s attempts=%s code=%s terminal=%s", job["id"], job["kind"], job["attempts"], code, state == "FAILED")


def process_job(job):
    try:
        with Lease(job) as lease:
            if job["kind"] == "SCAN":
                scan(job, lease)
            elif job["kind"] == "PURGE":
                purge(job, lease)
            elif job["kind"] == "CLEANUP":
                cleanup(job, lease)
            elif job["kind"] == "COMPLETE":
                lease.check()
                finish_upload(job["upload_id"])
                with connection() as conn:
                    done(conn, job)
            else:
                raise RuntimeError("Unknown job kind")
        log.info("job_processed job_id=%s kind=%s", job["id"], job["kind"])
    except LeaseLost:
        log.warning("job_lease_lost job_id=%s kind=%s", job["id"], job["kind"])
    except Exception as exc:
        # Log type, never keys, file content, presigned URLs, or raw SDK errors.
        log.warning("job_exception job_id=%s exception=%s", job["id"], type(exc).__name__)
        try:
            fail(job, exc)
        except LeaseLost:
            log.warning("job_failure_lease_lost job_id=%s", job["id"])
        except Exception:
            log.error("job_failure_persistence_unavailable job_id=%s", job["id"])


def main():
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    last_reconcile = 0.0
    while not STOP.is_set():
        try:
            if time.monotonic() - last_reconcile > 30:
                reconcile()
                last_reconcile = time.monotonic()
            job = claim_job()
            health_heartbeat()
            if job:
                process_job(job)
            else:
                STOP.wait(1)
        except Exception as exc:
            log.error("worker_dependency_unavailable exception=%s", type(exc).__name__)
            STOP.wait(5)


if __name__ == "__main__":
    main()
