"""Multipart sessions, immutable versions, and short-lived read capabilities."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from psycopg.types.json import Jsonb

from . import storage
from .db import connection
from .errors import Problem
from .security import (
    Actor, audit, file_access, get_actor, idempotency_get, idempotency_put,
    normalize_name, workspace_access,
)

router = APIRouter()


class UploadBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sizeBytes: int = Field(ge=1, le=2147483648)
    sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    contentType: str = Field(min_length=1, max_length=200)


class NewUploadBody(UploadBody):
    name: str = Field(min_length=1, max_length=255)


class VersionUploadBody(UploadBody):
    expectedVersionNo: int = Field(ge=1)


class Part(BaseModel):
    model_config = ConfigDict(extra="forbid")
    partNumber: int = Field(ge=1, le=10000)
    etag: str = Field(min_length=1, max_length=200)


class CompleteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parts: list[Part] = Field(min_length=1, max_length=10000)


class RestoreBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expectedVersionNo: int = Field(ge=1)


def utcnow():
    return datetime.now(timezone.utc)


def session_json(row):
    state = row["state"]
    if state == "INITIATING":
        state = "INITIATED"
    elif state == "COMPLETING":
        state = "PROCESSING"
    return dict(
        uploadId=str(row["id"]), status=state,
        partSizeBytes=row["part_size_bytes"], partCount=row["part_count"],
        expiresAt=row["expires_at"].isoformat(),
        fileId=str(row["file_id"]) if row["file_id"] else None,
        versionNo=row["version_no"], errorCode=row["error_code"],
    )


def version_json(row):
    return dict(versionNo=row["version_no"], sizeBytes=row["size_bytes"],
                sha256=row["sha256"], contentType=row["content_type"],
                status=row["scan_status"], createdAt=row["created_at"].isoformat(),
                createdBy=str(row["created_by"]))


def _key(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError):
        raise Problem(400, "INVALID_IDEMPOTENCY_KEY", "Idempotency-Key must be a UUID.")


def _hash(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def enqueue(conn, kind, target_id, upload_id=None, generation=0, delay=0):
    conn.execute(
        """INSERT INTO jobs(id,kind,target_id,upload_id,generation,next_run_at)
           VALUES (%s,%s,%s,%s,%s,now() + make_interval(secs => %s))
           ON CONFLICT(kind,target_id,generation) DO NOTHING""",
        (uuid4(), kind, target_id, upload_id, generation, delay),
    )


def _upload_access(conn, actor, upload_id, lock=False):
    upload = conn.execute("SELECT * FROM upload_sessions WHERE id=%s", (upload_id,)).fetchone()
    if not upload:
        raise Problem(404, "NOT_FOUND", "Upload session not found.")
    if upload["actor_id"] != actor.id:
        raise Problem(403, "ACCESS_DENIED", "Only the upload owner may use this session.")
    if upload["file_id"]:
        file_access(conn, actor, upload["file_id"], minimum="EDITOR", lock="update" if lock else "share")
    else:
        workspace_access(conn, actor, upload["workspace_id"], minimum="EDITOR", lock="update" if lock else "share")
    if lock:
        upload = conn.execute("SELECT * FROM upload_sessions WHERE id=%s FOR UPDATE", (upload_id,)).fetchone()
    return upload


def _initiate(actor, request, body, key, folder_id=None, file_id=None):
    key = _key(key)
    operation = f"init-upload:{'file' if file_id else 'folder'}:{file_id or folder_id}"
    durable_key = operation + ":" + key
    request_body = body.model_dump()
    fingerprint = _hash({"body": request_body, "folder": str(folder_id), "file": str(file_id)})
    with connection() as conn:
        # The same key cannot reserve quota twice, even before the S3 upload ID
        # has been recorded. The durable session remains retryable after a crash.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (str(actor.id) + key,))
        prior = conn.execute("SELECT * FROM upload_sessions WHERE actor_id=%s AND init_key=%s", (actor.id, durable_key)).fetchone()
        if prior:
            if prior["init_hash"] != fingerprint:
                raise Problem(409, "IDEMPOTENCY_KEY_REUSED", "This key was used for another upload request.")
            upload = _upload_access(conn, actor, prior["id"], lock=True)
            cached = idempotency_get(conn, actor.id, operation, key, request_body)
            if cached:
                return cached
        else:
            if file_id:
                file = file_access(conn, actor, file_id, minimum="EDITOR", lock="update")
                folder_id, workspace_id = file["folder_id"], file["workspace_id"]
                name, normalized = file["name"], file["normalized_name"]
                expected = body.expectedVersionNo
            else:
                folder = conn.execute("SELECT * FROM folders WHERE id=%s", (folder_id,)).fetchone()
                if not folder:
                    raise Problem(404, "NOT_FOUND", "Folder not found.")
                workspace_id = folder["workspace_id"]
                workspace_access(conn, actor, workspace_id, minimum="EDITOR", lock="update")
                name, normalized, expected = body.name.strip(), normalize_name(body.name), None
                occupied = conn.execute(
                    """SELECT 1 FROM files WHERE folder_id=%s AND normalized_name=%s AND purge_state='ACTIVE'
                       UNION ALL SELECT 1 FROM folders WHERE parent_id=%s AND normalized_name=%s""",
                    (folder_id, normalized, folder_id, normalized),
                ).fetchone()
                if occupied:
                    raise Problem(409, "NAME_CONFLICT", "An item with that name already exists in this folder.")
            workspace = conn.execute("SELECT * FROM workspaces WHERE id=%s FOR UPDATE", (workspace_id,)).fetchone()
            if workspace["used_bytes"] + workspace["reserved_bytes"] + body.sizeBytes > workspace["quota_bytes"]:
                raise Problem(409, "QUOTA_EXCEEDED", "The workspace does not have enough free quota.")
            conn.execute("SELECT pg_advisory_xact_lock(438927106)")
            active_count = conn.execute(
                "SELECT count(*) AS count FROM upload_sessions WHERE reserved_bytes>0"
            ).fetchone()["count"]
            if active_count >= 20:
                raise Problem(429, "TOO_MANY_UPLOADS", "Twenty uploads are already active. Try again shortly.")
            upload_id = uuid4()
            upload = conn.execute(
                """INSERT INTO upload_sessions
                   (id,workspace_id,folder_id,file_id,actor_id,name,normalized_name,object_key,
                    size_bytes,sha256,content_type,expected_version_no,state,part_size_bytes,
                    part_count,reserved_bytes,expires_at,init_key,init_hash)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'INITIATING',%s,%s,%s,%s,%s,%s)
                   RETURNING *""",
                (upload_id, workspace_id, folder_id, file_id, actor.id, name, normalized,
                 f"{workspace_id}/{upload_id}", body.sizeBytes, body.sha256.lower(), body.contentType,
                 expected, storage.PART_SIZE, math.ceil(body.sizeBytes / storage.PART_SIZE),
                 body.sizeBytes, utcnow() + timedelta(hours=24), durable_key, fingerprint),
            ).fetchone()
            conn.execute("UPDATE workspaces SET reserved_bytes=reserved_bytes+%s WHERE id=%s", (body.sizeBytes, workspace_id))
            audit(conn, actor.id, "UPLOAD_INITIATED", upload_id, request.state.request_id,
                  workspace_id=workspace_id, effect_key=f"upload-init:{upload_id}")
    if upload["state"] != "INITIATING":
        return session_json(upload)
    try:
        multipart_id = storage.create_multipart(upload)
    except Exception as exc:
        raise Problem(503, "STORAGE_UNAVAILABLE", "Storage is unavailable. Retry with the same idempotency key.") from exc
    with connection() as conn:
        current = _upload_access(conn, actor, upload["id"], lock=True)
        if current["state"] == "INITIATING":
            current = conn.execute(
                "UPDATE upload_sessions SET storage_upload_id=%s,state='INITIATED' WHERE id=%s RETURNING *",
                (multipart_id, upload["id"]),
            ).fetchone()
        result = session_json(current)
        cached = idempotency_get(conn, actor.id, operation, key, request_body)
        if cached:
            result = cached
        else:
            idempotency_put(conn, actor.id, operation, key, request_body, result, status=201)
    # A duplicate initiation may create an empty multipart, which expiry cleanup
    # aborts by its unique object key. Only the persisted upload ID is signed.
    return result


@router.post("/folders/{folder_id}/uploads", status_code=201)
def new_upload(folder_id: UUID, body: NewUploadBody, request: Request,
               actor: Actor = Depends(get_actor), idempotency_key: str = Header(alias="Idempotency-Key")):
    return _initiate(actor, request, body, idempotency_key, folder_id=folder_id)


@router.post("/files/{file_id}/uploads", status_code=201)
def new_version_upload(file_id: UUID, body: VersionUploadBody, request: Request,
                       actor: Actor = Depends(get_actor), idempotency_key: str = Header(alias="Idempotency-Key")):
    return _initiate(actor, request, body, idempotency_key, file_id=file_id)


@router.get("/uploads/{upload_id}/parts/{part_number}/url")
def signed_part(upload_id: UUID, part_number: int, response: Response, actor: Actor = Depends(get_actor)):
    with connection() as conn:
        upload = _upload_access(conn, actor, upload_id)
        if upload["state"] != "INITIATED" or upload["expires_at"] <= utcnow():
            raise Problem(409, "UPLOAD_NOT_ACTIVE", "The upload is no longer accepting parts.")
        if part_number < 1 or part_number > upload["part_count"]:
            raise Problem(400, "INVALID_PART", "Part number is outside the session's range.")
        expires = max(1, min(900, int((upload["expires_at"] - utcnow()).total_seconds())))
        url = storage.part_url(upload, part_number, expires)
        response.headers["Cache-Control"] = "no-store"
        return dict(method="PUT", url=url, expiresAt=(utcnow() + timedelta(seconds=expires)).isoformat(),
                    requiredHeaders={"Content-Type": upload["content_type"]})


def _actor_from_database(conn, user_id):
    user = conn.execute("SELECT * FROM users WHERE id=%s", (user_id,)).fetchone()
    if not user or user["status"] != "ACTIVE":
        raise Problem(403, "ACCESS_DENIED", "The upload owner is no longer active.")
    return Actor(id=user["id"], email=user["email"], display_name=user["display_name"],
                 security_admin=user["security_admin"], csrf_token="")


def finish_upload(upload_id):
    """Reconcile durable COMPLETING state; safe to repeat after any crash."""
    with connection() as conn:
        upload = conn.execute("SELECT * FROM upload_sessions WHERE id=%s", (upload_id,)).fetchone()
    if not upload or upload["state"] != "COMPLETING":
        return upload
    storage.complete_multipart(upload)
    with connection() as conn:
        # Workspace first is the common lock ordering for ACL/quota/file changes.
        conn.execute("SELECT id FROM workspaces WHERE id=%s FOR UPDATE", (upload["workspace_id"],))
        current = conn.execute("SELECT * FROM upload_sessions WHERE id=%s FOR UPDATE", (upload_id,)).fetchone()
        if current["state"] != "COMPLETING":
            return current
        error_code = None
        try:
            actor = _actor_from_database(conn, upload["actor_id"])
            if upload["file_id"]:
                file = file_access(conn, actor, upload["file_id"], minimum="EDITOR", lock="update")
                if file["latest_version_no"] != upload["expected_version_no"]:
                    error_code = "VERSION_CONFLICT"
            else:
                workspace_access(conn, actor, upload["workspace_id"], minimum="EDITOR", lock="update")
                occupied = conn.execute(
                    """SELECT 1 FROM files WHERE folder_id=%s AND normalized_name=%s AND purge_state='ACTIVE'
                       UNION ALL SELECT 1 FROM folders WHERE parent_id=%s AND normalized_name=%s""",
                    (upload["folder_id"], upload["normalized_name"], upload["folder_id"], upload["normalized_name"]),
                ).fetchone()
                if occupied:
                    error_code = "NAME_CONFLICT"
        except Problem as exc:
            error_code = exc.code
        if error_code:
            current = conn.execute(
                "UPDATE upload_sessions SET state='CONFLICT',error_code=%s WHERE id=%s RETURNING *",
                (error_code, upload_id),
            ).fetchone()
            enqueue(conn, "CLEANUP", upload_id, upload_id)
            audit(conn, upload["actor_id"], "UPLOAD_CONFLICT", upload_id, uuid4(), result="DENIED",
                  workspace_id=upload["workspace_id"], effect_key=f"upload-conflict:{upload_id}")
            return current
        if upload["file_id"]:
            version_no = upload["expected_version_no"] + 1
            changed = conn.execute(
                "UPDATE files SET latest_version_no=%s,updated_at=now() WHERE id=%s AND latest_version_no=%s RETURNING id",
                (version_no, upload["file_id"], upload["expected_version_no"]),
            ).fetchone()
            if not changed:
                raise RuntimeError("File changed despite holding its row lock")
            file_id = upload["file_id"]
        else:
            version_no, file_id = 1, uuid4()
            conn.execute(
                """INSERT INTO files(id,workspace_id,folder_id,name,normalized_name,latest_version_no,created_by)
                   VALUES (%s,%s,%s,%s,%s,1,%s)""",
                (file_id, upload["workspace_id"], upload["folder_id"], upload["name"], upload["normalized_name"], upload["actor_id"]),
            )
        object_id = uuid4()
        conn.execute(
            "INSERT INTO storage_objects(id,workspace_id,object_key,size_bytes,sha256,content_type) VALUES (%s,%s,%s,%s,%s,%s)",
            (object_id, upload["workspace_id"], upload["object_key"], upload["size_bytes"], upload["sha256"], upload["content_type"]),
        )
        conn.execute(
            "INSERT INTO file_versions(file_id,version_no,object_id,created_by,upload_id) VALUES (%s,%s,%s,%s,%s)",
            (file_id, version_no, object_id, upload["actor_id"], upload_id),
        )
        conn.execute(
            "UPDATE workspaces SET used_bytes=used_bytes+%s,reserved_bytes=reserved_bytes-%s WHERE id=%s",
            (upload["size_bytes"], current["reserved_bytes"], upload["workspace_id"]),
        )
        current = conn.execute(
            "UPDATE upload_sessions SET file_id=%s,version_no=%s,state='PROCESSING',reserved_bytes=0 WHERE id=%s RETURNING *",
            (file_id, version_no, upload_id),
        ).fetchone()
        enqueue(conn, "SCAN", object_id, upload_id)
        audit(conn, upload["actor_id"], "UPLOAD_COMPLETED", file_id, uuid4(),
              workspace_id=upload["workspace_id"], effect_key=f"upload-complete:{upload_id}")
        # Store the accepted response in the same transaction as the version so
        # a rapid scan cannot change a retry's original 202 response.
        operation = f"complete-upload:{upload_id}"
        request_body = {"parts": current["parts"]}
        cached = idempotency_get(conn, upload["actor_id"], operation, current["completion_key"], request_body)
        if not cached:
            idempotency_put(conn, upload["actor_id"], operation, current["completion_key"],
                            request_body, session_json(current), status=202)
        return current


@router.post("/uploads/{upload_id}/complete", status_code=202)
def complete_upload(upload_id: UUID, body: CompleteBody, request: Request,
                    actor: Actor = Depends(get_actor), idempotency_key: str = Header(alias="Idempotency-Key")):
    key = _key(idempotency_key)
    parts = [part.model_dump() for part in body.parts]
    fingerprint = _hash(parts)
    with connection() as conn:
        upload = _upload_access(conn, actor, upload_id, lock=True)
        cached = idempotency_get(conn, actor.id, f"complete-upload:{upload_id}", key, body.model_dump())
        if cached:
            return cached
        if upload["completion_hash"] and upload["completion_hash"] != fingerprint:
            raise Problem(409, "IDEMPOTENCY_KEY_REUSED", "This upload was completed with a different manifest.")
        if upload["state"] == "CONFLICT":
            raise Problem(409, upload["error_code"], "The upload could not commit; preserve your local copy.")
        if upload["state"] in {"PROCESSING", "READY", "REJECTED"}:
            return session_json(upload)
        if upload["state"] not in {"INITIATED", "COMPLETING"} or (
                upload["expires_at"] <= utcnow() and upload["state"] != "COMPLETING"):
            raise Problem(409, "UPLOAD_EXPIRED", "This upload session is no longer active.")
    if upload["state"] == "INITIATED":
        try:
            storage.validate_parts(upload, parts)
        except storage.InvalidManifest as exc:
            raise Problem(422, "INVALID_PARTS", str(exc)) from exc
        except Exception as exc:
            raise Problem(503, "STORAGE_UNAVAILABLE", "Could not verify upload parts; retry shortly.") from exc
        with connection() as conn:
            current = _upload_access(conn, actor, upload_id, lock=True)
            if current["completion_hash"] and current["completion_hash"] != fingerprint:
                raise Problem(409, "IDEMPOTENCY_KEY_REUSED", "A different manifest already completed this session.")
            if current["state"] == "INITIATED" and current["expires_at"] > utcnow():
                conn.execute(
                    """UPDATE upload_sessions SET state='COMPLETING',parts=%s,completion_key=%s,
                       completion_hash=%s,complete_started_at=now() WHERE id=%s""",
                    (Jsonb(parts), key, fingerprint, upload_id),
                )
                enqueue(conn, "COMPLETE", upload_id, upload_id, delay=30)
            elif current["state"] not in {"COMPLETING", "PROCESSING", "READY", "REJECTED"}:
                raise Problem(409, "UPLOAD_NOT_ACTIVE", "The upload is no longer active.")
    try:
        result = finish_upload(upload_id)
    except storage.InvalidManifest as exc:
        # Keep a durable operation for inspection/retry; never expose its object.
        raise Problem(422, "INVALID_PARTS", str(exc)) from exc
    except Exception as exc:
        raise Problem(503, "STORAGE_UNAVAILABLE", "Completion is being retried. Poll this upload before retrying.") from exc
    if result["state"] == "CONFLICT":
        raise Problem(409, result["error_code"], "Another change won this update. Save your changes as a separate file.")
    return session_json(result)


@router.get("/uploads/{upload_id}")
def get_upload(upload_id: UUID, actor: Actor = Depends(get_actor)):
    with connection() as conn:
        return session_json(_upload_access(conn, actor, upload_id))


def download_for_file(conn, file_row, actor, request_id):
    # Callers must hold the workspace ACL lock and check the effective grant.
    if file_row["purge_state"] != "ACTIVE":
        raise Problem(409, "FILE_NOT_ACTIVE", "Restore this file from trash before downloading it.")
    version = conn.execute(
        """SELECT v.version_no,o.object_key FROM file_versions v JOIN storage_objects o ON o.id=v.object_id
           WHERE v.file_id=%s AND v.scan_status='CLEAN' AND o.state='AVAILABLE'
           ORDER BY v.version_no DESC LIMIT 1""", (file_row["id"],)
    ).fetchone()
    if not version:
        latest = conn.execute(
            "SELECT scan_status FROM file_versions WHERE file_id=%s ORDER BY version_no DESC LIMIT 1", (file_row["id"],)
        ).fetchone()
        if latest and latest["scan_status"] == "REJECTED":
            raise Problem(422, "QUARANTINED", "This file did not pass checksum or antivirus verification.")
        raise Problem(409, "FILE_NOT_READY", "No version has passed checksum and antivirus verification yet.")
    url = storage.download_url(version["object_key"], file_row["name"])
    audit(conn, actor.id, "DOWNLOAD_LINK_ISSUED", file_row["id"], request_id, workspace_id=file_row["workspace_id"])
    return dict(method="GET", url=url, versionNo=version["version_no"], expiresAt=(utcnow() + timedelta(seconds=60)).isoformat())


@router.get("/files/{file_id}/download-link")
def download_link(file_id: UUID, request: Request, response: Response, actor: Actor = Depends(get_actor)):
    with connection() as conn:
        file = file_access(conn, actor, file_id, lock="share")
        result = download_for_file(conn, file, actor, request.state.request_id)
    response.headers["Cache-Control"] = "no-store"
    return result


@router.post("/files/{file_id}/versions/{version_no}/restore", status_code=201)
def restore_version(file_id: UUID, version_no: int, body: RestoreBody, request: Request,
                    actor: Actor = Depends(get_actor), idempotency_key: str = Header(alias="Idempotency-Key")):
    operation = f"restore-version:{file_id}:{version_no}"
    with connection() as conn:
        file = file_access(conn, actor, file_id, minimum="EDITOR", lock="update")
        cached = idempotency_get(conn, actor.id, operation, idempotency_key, body.model_dump())
        if cached:
            return cached
        if file["latest_version_no"] != body.expectedVersionNo:
            raise Problem(409, "VERSION_CONFLICT", "The file changed since you opened it.", currentVersionNo=file["latest_version_no"])
        version = conn.execute(
            """SELECT v.*,o.size_bytes,o.sha256,o.content_type,o.state FROM file_versions v
               JOIN storage_objects o ON o.id=v.object_id WHERE v.file_id=%s AND v.version_no=%s""",
            (file_id, version_no),
        ).fetchone()
        if not version:
            raise Problem(404, "NOT_FOUND", "Version not found.")
        if version["scan_status"] != "CLEAN" or version["state"] != "AVAILABLE":
            raise Problem(422, "VERSION_NOT_CLEAN", "Only a verified clean version can be restored.")
        latest = file["latest_version_no"] + 1
        conn.execute("UPDATE files SET latest_version_no=%s,updated_at=now() WHERE id=%s AND latest_version_no=%s",
                     (latest, file_id, body.expectedVersionNo))
        created = conn.execute(
            """INSERT INTO file_versions(file_id,version_no,object_id,scan_status,created_by)
               VALUES (%s,%s,%s,'CLEAN',%s) RETURNING *""", (file_id, latest, version["object_id"], actor.id),
        ).fetchone()
        result = version_json({**version, **created})
        audit(conn, actor.id, "VERSION_RESTORED", file_id, request.state.request_id,
              workspace_id=file["workspace_id"], effect_key=f"version-restore:{file_id}:{latest}")
        idempotency_put(conn, actor.id, operation, idempotency_key, body.model_dump(), result, status=201)
        return result
