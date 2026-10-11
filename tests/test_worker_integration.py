"""Failure injection against the real local Docker database, S3, and ClamAV.

Run separately from other integration tests: this module temporarily stops the
background worker and drives its production functions deterministically.
"""
import hashlib
import json
import subprocess
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest

from conftest import ROOT

pytestmark = pytest.mark.integration
WORKSPACE = "00000000-0000-4000-8000-000000000010"


def compose(*args, input=None):
    result = subprocess.run([str(ROOT / "scripts/compose.sh"), *args], input=input,
                            text=True, capture_output=True, timeout=90, cwd=ROOT)
    assert result.returncode == 0, result.stderr[-2000:] + result.stdout[-2000:]
    return result.stdout


def python(source):
    output = compose("exec", "-T", "api", "python", "-", input=(
        "import json\nfrom app.db import connection,close_pool\ntry:\n"
        + textwrap.indent(source, "    ") + "\nfinally:\n    close_pool()\n"
    ))
    return json.loads(output.strip().splitlines()[-1])


def db(statement, parameters=()):
    return python(
        f"with connection() as conn:\n    rows=conn.execute({statement!r}, {tuple(parameters)!r}).fetchall()\n"
        "print(json.dumps(rows,default=str))\n"
    )


def scan_job(upload):
    return db("SELECT * FROM jobs WHERE kind='SCAN' AND upload_id=%s", (upload["uploadId"],))[0]


def claim(job):
    return python(
        "from app.worker import claim_job\n"
        f"with connection() as conn:\n    conn.execute(\"UPDATE jobs SET next_run_at='1970-01-01' WHERE id=%s\", ({job['id']!r},))\n"
        "print(json.dumps(claim_job(),default=str))\n"
    )


def process(job):
    return python(
        "from app.worker import process_job\n"
        f"process_job({job!r})\n"
        f"with connection() as conn:\n    row=conn.execute('SELECT * FROM jobs WHERE id=%s',({job['id']!r},)).fetchone()\n"
        "print(json.dumps(row,default=str))\n"
    )


def prepared(owner, data=b"Failure injection fixture\n"):
    session, parts = owner.prepare(data)
    accepted = owner.complete(session, parts)
    return session, accepted


def ready(owner):
    session, accepted = prepared(owner)
    assert process(claim(scan_job(session)))["state"] == "DONE"
    owner.wait_scan(session)
    return session, accepted


@pytest.fixture(scope="module", autouse=True)
def controlled_worker(accounts):
    compose("stop", "worker")
    try:
        yield
    finally:
        compose("start", "worker")


def test_scanner_unavailable_keeps_pending_and_retries_safely(accounts):
    owner = accounts["owner"]
    session, accepted = prepared(owner)
    job = scan_job(session)
    for attempt in range(1, 4):
        first = claim(job)
        result = python(
            "import os\nfrom app.worker import process_job\n"
            "os.environ['CLAMAV_PORT']='9'\n"
            f"process_job({first!r})\n"
            f"with connection() as conn:\n    row=conn.execute('SELECT * FROM jobs WHERE id=%s',({first['id']!r},)).fetchone()\n"
            "print(json.dumps(row,default=str))\n"
        )
        assert result["state"] == ("FAILED" if attempt == 3 else "QUEUED")
        assert result["attempts"] == attempt
        assert result["error_code"] == "SCANNER_UNAVAILABLE"
    assert owner.get(f"/uploads/{session['uploadId']}")["status"] == "PROCESSING"
    owner.get(f"/files/{accepted['fileId']}/download-link", expected=409)
    accounts["security"].post(f"/admin/jobs/{job['id']}/retry")
    assert process(claim(job))["state"] == "DONE"
    assert owner.wait_scan(session)["status"] == "READY"


def test_reclaimed_lease_fences_old_scan_result_and_audit(accounts):
    owner = accounts["owner"]
    session, _ = prepared(owner)
    first = claim(scan_job(session))
    db("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=%s RETURNING id", (first["id"],))
    second = claim(first)
    assert second["id"] == first["id"]
    assert second["lease_token"] != first["lease_token"]
    assert second["attempts"] == 2
    result = python(
        "from app.worker import scan,LeaseLost\n"
        "class UnawareOldWorker:\n    def check(self): pass\n"
        f"try:\n    scan({first!r},UnawareOldWorker())\n    result='incorrectly committed'\n"
        "except LeaseLost:\n    result='fenced'\n"
        "print(json.dumps(result))\n"
    )
    assert result == "fenced"
    assert db("SELECT scan_status FROM file_versions WHERE upload_id=%s", (session["uploadId"],))[0]["scan_status"] == "PENDING_SCAN"
    assert not db("SELECT id FROM audit_events WHERE effect_key=%s", ("scan:" + session["uploadId"],))
    assert process(second)["state"] == "DONE"
    assert len(db("SELECT id FROM audit_events WHERE effect_key=%s", ("scan:" + session["uploadId"],))) == 1
    owner.wait_scan(session)


def test_expired_multipart_cleanup_releases_only_confirmed_reservation(accounts):
    owner = accounts["owner"]
    session, _ = owner.prepare(b"unfinished bytes")
    upload_id = session["uploadId"]
    before = db("SELECT reserved_bytes FROM workspaces WHERE id=%s", (WORKSPACE,))[0]["reserved_bytes"]
    upload = db("UPDATE upload_sessions SET expires_at=now()-interval '1 second' WHERE id=%s RETURNING *", (upload_id,))[0]
    python("from app.worker import reconcile\nreconcile()\nprint('null')\n")
    job = db("SELECT * FROM jobs WHERE kind='CLEANUP' AND upload_id=%s", (upload_id,))[0]
    assert process(claim(job))["state"] == "DONE"
    after = db("SELECT reserved_bytes FROM workspaces WHERE id=%s", (WORKSPACE,))[0]["reserved_bytes"]
    assert after == before - upload["reserved_bytes"]
    assert owner.get(f"/uploads/{upload_id}")["status"] == "EXPIRED"
    assert python(f"from app.storage import head\nprint(json.dumps(head({upload['object_key']!r}),default=str))\n") is None
    pending = python(
        "from app.storage import client,BUCKET\n"
        f"r=client().list_multipart_uploads(Bucket=BUCKET,Prefix={upload['object_key']!r})\n"
        "print(json.dumps(r.get('Uploads',[]),default=str))\n"
    )
    assert pending == []


def purge_job(file_id):
    return python(
        "from uuid import UUID\nfrom app.uploads import enqueue\n"
        f"with connection() as conn:\n    enqueue(conn,'PURGE',UUID({file_id!r}),generation=991)\n"
        f"with connection() as conn:\n    row=conn.execute(\"SELECT * FROM jobs WHERE kind='PURGE' AND target_id=%s AND generation=991\",({file_id!r},)).fetchone()\n"
        "print(json.dumps(row,default=str))\n"
    )


def test_hold_recover_and_irreversible_purge_preserve_invariants(accounts):
    owner, admin = accounts["owner"], accounts["security"]
    session, accepted = ready(owner)
    file_id = accepted["fileId"]
    obj = db("SELECT * FROM storage_objects WHERE object_key=(SELECT object_key FROM upload_sessions WHERE id=%s)", (session["uploadId"],))[0]
    admin.request("PATCH", f"/files/{file_id}/legal-hold", json={"legalHold": True})
    owner.request("DELETE", f"/files/{file_id}", expected=204)
    db("UPDATE files SET purge_at=now()-interval '1 second' WHERE id=%s RETURNING id", (file_id,))
    job = purge_job(file_id)
    held = process(claim(job))
    assert held["state"] == "QUEUED" and held["attempts"] == 0
    assert db("SELECT purge_state FROM files WHERE id=%s", (file_id,))[0]["purge_state"] == "TRASHED"
    assert python(f"from app.storage import head\nprint(json.dumps(head({obj['object_key']!r}) is not None))\n") is True
    # Recovery wins before the due time; a previously queued purge becomes inert.
    db("UPDATE files SET purge_at=now()+interval '1 day' WHERE id=%s RETURNING id", (file_id,))
    owner.post(f"/files/{file_id}/recover")
    assert process(claim(job))["state"] == "DONE"
    assert owner.get(f"/files/{file_id}/download-link")["versionNo"] == 1
    admin.request("PATCH", f"/files/{file_id}/legal-hold", json={"legalHold": False})
    owner.request("DELETE", f"/files/{file_id}", expected=204)
    db("UPDATE files SET purge_at=now()-interval '1 second' WHERE id=%s RETURNING id", (file_id,))
    # Reset only our test job for the new deletion generation.
    db("UPDATE jobs SET state='QUEUED',attempts=0 WHERE id=%s RETURNING id", (job["id"],))
    before = db("SELECT used_bytes FROM workspaces WHERE id=%s", (WORKSPACE,))[0]["used_bytes"]
    assert process(claim(job))["state"] == "DONE"
    assert db("SELECT purge_state FROM files WHERE id=%s", (file_id,))[0]["purge_state"] == "PURGED"
    assert python(f"from app.storage import head\nprint(json.dumps(head({obj['object_key']!r}),default=str))\n") is None
    assert db("SELECT used_bytes FROM workspaces WHERE id=%s", (WORKSPACE,))[0]["used_bytes"] == before - obj["size_bytes"]
    owner.post(f"/files/{file_id}/recover", expected=404)


def test_purge_transition_blocks_recover_and_new_hold(accounts):
    owner, admin = accounts["owner"], accounts["security"]
    _, accepted = ready(owner)
    file_id = accepted["fileId"]
    owner.request("DELETE", f"/files/{file_id}", expected=204)
    db("UPDATE files SET purge_at=now()-interval '1 second' WHERE id=%s RETURNING id", (file_id,))
    job = claim(purge_job(file_id))
    # Crash after irreversible CAS and physical delete but before DB finalize.
    # The retry must finish PURGED and account bytes exactly once.
    crashed = python(
        "from app import worker,storage\n"
        "class NoLeaseLoss:\n    def check(self): pass\n"
        "real_delete=storage.delete_confirmed\n"
        "def crash_after_delete(key):\n    real_delete(key)\n    raise RuntimeError('injected process failure')\n"
        "storage.delete_confirmed=crash_after_delete\n"
        f"try:\n    worker.purge({job!r},NoLeaseLoss())\n    result=False\nexcept RuntimeError:\n    result=True\n"
        "print(json.dumps(result))\n"
    )
    assert crashed is True
    assert db("SELECT purge_state FROM files WHERE id=%s", (file_id,))[0]["purge_state"] == "PURGING"
    owner.post(f"/files/{file_id}/recover", expected=409)
    admin.request("PATCH", f"/files/{file_id}/legal-hold", json={"legalHold": True}, expected=409)
    db("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=%s RETURNING id", (job["id"],))
    assert process(claim(job))["state"] == "DONE"
    assert db("SELECT purge_state FROM files WHERE id=%s", (file_id,))[0]["purge_state"] == "PURGED"
    assert len(db("SELECT id FROM audit_events WHERE effect_key=%s", ("purge:" + file_id,))) == 1


def test_complete_recovers_after_s3_success_before_database_commit(accounts):
    owner = accounts["owner"]
    session, manifest = owner.prepare(b"Durable complete crash boundary\n")
    upload_id = session["uploadId"]
    # Persist the exact operation and complete S3, then exit before metadata.
    python(
        "from psycopg.types.json import Jsonb\nfrom app import storage\nfrom app.uploads import _hash,enqueue\n"
        f"parts={manifest['parts']!r}\n"
        f"with connection() as conn:\n    row=conn.execute(\"UPDATE upload_sessions SET state='COMPLETING',parts=%s,completion_key=%s,completion_hash=%s,complete_started_at=now() WHERE id=%s RETURNING *\",(Jsonb(parts),{str(uuid4())!r},_hash(parts),{upload_id!r})).fetchone()\n    enqueue(conn,'COMPLETE',row['id'],row['id'])\n"
        "storage.complete_multipart(row)\nprint('null')\n"
    )
    assert db("SELECT version_no FROM upload_sessions WHERE id=%s", (upload_id,))[0]["version_no"] is None
    job = db("SELECT * FROM jobs WHERE kind='COMPLETE' AND upload_id=%s", (upload_id,))[0]
    assert process(claim(job))["state"] == "DONE"
    assert len(db("SELECT * FROM file_versions WHERE upload_id=%s", (upload_id,))) == 1
    assert process(claim(scan_job(session)))["state"] == "DONE"
    assert owner.wait_scan(session)["status"] == "READY"


def test_concurrent_reservations_cannot_exceed_workspace_quota(accounts):
    owner, admin = accounts["owner"], accounts["security"]
    policy = admin.get(f"/workspaces/{WORKSPACE}/policy")
    before = db("SELECT used_bytes,reserved_bytes FROM workspaces WHERE id=%s", (WORKSPACE,))[0]
    size = 17
    limit = before["used_bytes"] + before["reserved_bytes"] + size
    created = []
    try:
        admin.request("PUT", f"/workspaces/{WORKSPACE}/policy",
                      json={"quotaBytes": limit, "retentionDays": policy["retentionDays"]})
        def initiate(_):
            return owner.client.post("/api/v1/folders/00000000-0000-4000-8000-000000000011/uploads",
                headers={"Idempotency-Key": str(uuid4())},
                json={"name": "quota-race-" + uuid4().hex, "sizeBytes": size,
                      "sha256": hashlib.sha256(b"x" * size).hexdigest(), "contentType": "text/plain"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(initiate, range(2)))
        created = [response.json() for response in results if response.status_code == 201]
        assert sorted(response.status_code for response in results) == [201, 409]
        assert next(response.json() for response in results if response.status_code == 409)["code"] == "QUOTA_EXCEEDED"
        after = db("SELECT used_bytes,reserved_bytes,quota_bytes FROM workspaces WHERE id=%s", (WORKSPACE,))[0]
        assert after["used_bytes"] + after["reserved_bytes"] == after["quota_bytes"] == limit
    finally:
        admin.request("PUT", f"/workspaces/{WORKSPACE}/policy", json=policy)
        for session in created:
            db("UPDATE upload_sessions SET expires_at=now()-interval '1 second' WHERE id=%s RETURNING id", (session["uploadId"],))
        if created:
            python("from app.worker import reconcile\nreconcile()\nprint('null')\n")
            for session in created:
                job = db("SELECT * FROM jobs WHERE kind='CLEANUP' AND upload_id=%s", (session["uploadId"],))[0]
                assert process(claim(job))["state"] == "DONE"


def test_parallel_complete_with_distinct_keys_commits_exactly_once(accounts):
    owner = accounts["owner"]
    session, parts = owner.prepare(b"One durable version across parallel completion requests\n")
    before = db("SELECT used_bytes FROM workspaces WHERE id=%s", (WORKSPACE,))[0]["used_bytes"]
    def complete(_):
        return owner.client.post(f"/api/v1/uploads/{session['uploadId']}/complete", json=parts,
                                 headers={"Idempotency-Key": str(uuid4())})
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(complete, range(2)))
    assert all(response.status_code == 202 for response in outcomes), [response.text for response in outcomes]
    assert len(db("SELECT * FROM file_versions WHERE upload_id=%s", (session["uploadId"],))) == 1
    assert len(db("SELECT * FROM jobs WHERE kind='SCAN' AND upload_id=%s", (session["uploadId"],))) == 1
    upload = db("SELECT * FROM upload_sessions WHERE id=%s", (session["uploadId"],))[0]
    assert upload["reserved_bytes"] == 0
    assert db("SELECT used_bytes FROM workspaces WHERE id=%s", (WORKSPACE,))[0]["used_bytes"] == before + upload["size_bytes"]
    assert process(claim(scan_job(session)))["state"] == "DONE"


def test_signed_part_rejects_bytes_exceeding_reserved_size(accounts):
    owner = accounts["owner"]
    session, parts = owner.prepare(b"exact")
    signed = owner.get(f"/uploads/{session['uploadId']}/parts/1/url")
    response = httpx.put(signed["url"], content=b"oversized body", headers=signed["requiredHeaders"])
    assert response.status_code == 403
    owner.complete(session, parts)
    assert process(claim(scan_job(session)))["state"] == "DONE"


def test_acl_revocation_denies_new_links_and_old_capability_expires(accounts):
    owner, finance = accounts["owner"], accounts["finance"]
    _, accepted = ready(owner)
    file_id = accepted["fileId"]
    user_id = "00000000-0000-4000-8000-000000000005"
    owner.request("PUT", f"/files/{file_id}/permissions/{user_id}", json={"role": "VIEWER"})
    signed = finance.get(f"/files/{file_id}/download-link")
    issued = time.monotonic()
    owner.request("DELETE", f"/files/{file_id}/permissions/{user_id}", expected=204)
    finance.get(f"/files/{file_id}/download-link", expected=403)
    assert httpx.get(signed["url"]).status_code == 200
    # The already-issued capability remains usable for its documented TTL.
    while time.monotonic() - issued < 50:
        time.sleep(min(5, 50 - (time.monotonic() - issued)))
    assert httpx.get(signed["url"]).status_code == 200
    while time.monotonic() - issued < 62:
        time.sleep(min(5, 62 - (time.monotonic() - issued)))
    assert httpx.get(signed["url"]).status_code == 403


def test_global_twenty_active_upload_limit_and_cleanup(accounts):
    owner = accounts["owner"]
    baseline = db("SELECT count(*) AS count FROM upload_sessions WHERE reserved_bytes>0")[0]["count"]
    assert baseline <= 20
    created = []
    body = {"sizeBytes": 1, "sha256": hashlib.sha256(b"x").hexdigest(), "contentType": "text/plain"}
    path = "/folders/00000000-0000-4000-8000-000000000011/uploads"
    try:
        for _ in range(20 - baseline):
            created.append(owner.post(path, json={**body, "name": "capacity-" + uuid4().hex}, expected=201))
        failure = owner.post(path, json={**body, "name": "capacity-denied-" + uuid4().hex}, expected=429)
        assert failure["code"] == "TOO_MANY_UPLOADS"
        assert db("SELECT count(*) AS count FROM upload_sessions WHERE reserved_bytes>0")[0]["count"] == 20
    finally:
        for session in created:
            db("UPDATE upload_sessions SET expires_at=now()-interval '1 second' WHERE id=%s RETURNING id", (session["uploadId"],))
        if created:
            python("from app.worker import reconcile\nreconcile()\nprint('null')\n")
            for session in created:
                job = db("SELECT * FROM jobs WHERE kind='CLEANUP' AND upload_id=%s", (session["uploadId"],))[0]
                assert process(claim(job))["state"] == "DONE"
    assert db("SELECT count(*) AS count FROM upload_sessions WHERE reserved_bytes>0")[0]["count"] == baseline
