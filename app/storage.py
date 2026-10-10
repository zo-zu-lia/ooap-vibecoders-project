"""Private S3 transfers and bounded-memory ClamAV verification.

Only the API signs public URLs. Workers access the internal endpoint; neither
objects nor multipart uploads are made public by bucket policy.
"""

from __future__ import annotations

import hashlib
import os
import socket
import struct
import time
from functools import lru_cache
from typing import Callable
from urllib.parse import quote

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

BUCKET = os.getenv("S3_BUCKET", "fileflow")
PART_SIZE = 8 * 1024 * 1024
CHUNK_SIZE = 256 * 1024


class InvalidManifest(ValueError):
    pass


class ScannerUnavailable(RuntimeError):
    pass


@lru_cache(maxsize=2)
def client(public: bool = False):
    endpoint = os.getenv(
        "S3_PUBLIC_ENDPOINT" if public else "S3_ENDPOINT",
        "http://localhost:9000" if public else "http://storage:9000",
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["S3_ACCESS_KEY"],
        aws_secret_access_key=os.environ["S3_SECRET_KEY"],
        region_name=os.getenv("S3_REGION", "us-east-1"),
        config=Config(
            signature_version="s3v4",
            connect_timeout=3,
            read_timeout=20,
            max_pool_connections=4,
            retries={"max_attempts": 2, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )


def is_missing(exc: ClientError) -> bool:
    return exc.response.get("Error", {}).get("Code") in {
        "NoSuchKey", "NoSuchUpload", "NoSuchBucket", "404", "NotFound"
    }


def initialize_bucket() -> None:
    s3 = client()
    try:
        s3.head_bucket(Bucket=BUCKET)
    except ClientError as exc:
        if not is_missing(exc):
            raise
        try:
            s3.create_bucket(Bucket=BUCKET)
        except ClientError as create_exc:
            if create_exc.response["Error"]["Code"] not in {
                "BucketAlreadyOwnedByYou", "BucketAlreadyExists"
            }:
                raise
    # MinIO configures CORS globally; AWS S3 also supports per-bucket rules.
    origins = os.getenv("APP_ORIGIN", "http://localhost:8080").split(",")
    try:
        s3.put_bucket_cors(
            Bucket=BUCKET,
            CORSConfiguration={"CORSRules": [{
                "AllowedOrigins": origins,
                "AllowedMethods": ["GET", "HEAD", "PUT"],
                "AllowedHeaders": ["*"],
                "ExposeHeaders": ["ETag"],
                "MaxAgeSeconds": 300,
            }]},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in {"NotImplemented", "XNotImplemented"}:
            raise


def create_multipart(upload: dict) -> str:
    return client().create_multipart_upload(
        Bucket=BUCKET,
        Key=upload["object_key"],
        ContentType=upload["content_type"],
        Metadata={"upload-id": str(upload["id"]), "sha256": upload["sha256"]},
    )["UploadId"]


def part_url(upload: dict, part: int, expires: int = 900) -> str:
    return client(True).generate_presigned_url(
        "upload_part",
        Params={"Bucket": BUCKET, "Key": upload["object_key"],
                "UploadId": upload["storage_upload_id"], "PartNumber": part},
        ExpiresIn=expires,
        HttpMethod="PUT",
    )


def head(key: str) -> dict | None:
    try:
        return client().head_object(Bucket=BUCKET, Key=key)
    except ClientError as exc:
        if is_missing(exc):
            return None
        raise


def validate_parts(upload: dict, parts: list[dict]) -> None:
    if [part["partNumber"] for part in parts] != list(range(1, upload["part_count"] + 1)):
        raise InvalidManifest("Supply each part exactly once, in ascending order.")
    actual = []
    paginator = client().get_paginator("list_parts")
    for page in paginator.paginate(
        Bucket=BUCKET, Key=upload["object_key"], UploadId=upload["storage_upload_id"]
    ):
        actual.extend(page.get("Parts", []))
    if len(actual) != len(parts):
        raise InvalidManifest("The uploaded parts do not match the manifest.")
    for i, (given, stored) in enumerate(zip(parts, actual)):
        expected_size = min(upload["part_size_bytes"], upload["size_bytes"] - i * upload["part_size_bytes"])
        if (stored["PartNumber"] != given["partNumber"]
                or stored["ETag"].strip('"') != given["etag"].strip('"')
                or stored["Size"] != expected_size):
            raise InvalidManifest("Part number, ETag, or size does not match the uploaded bytes.")


def complete_multipart(upload: dict) -> None:
    # HEAD reconciles both a lost S3 response and an API crash before DB commit.
    obj = head(upload["object_key"])
    if obj is None:
        try:
            validate_parts(upload, upload["parts"])
            client().complete_multipart_upload(
                Bucket=BUCKET, Key=upload["object_key"], UploadId=upload["storage_upload_id"],
                MultipartUpload={"Parts": [
                    {"PartNumber": p["partNumber"], "ETag": p["etag"]} for p in upload["parts"]
                ]},
            )
        except Exception:
            # Never assume an exception means the operation did not commit.
            if head(upload["object_key"]) is None:
                raise
        obj = head(upload["object_key"])
    if (not obj or obj["ContentLength"] != upload["size_bytes"]
            or obj.get("Metadata", {}).get("upload-id") != str(upload["id"])):
        raise InvalidManifest("Completed object identity or size did not match the session.")
    abort_multiparts(upload["object_key"])


def download_url(key: str, filename: str) -> str:
    return client(True).generate_presigned_url(
        "get_object",
        Params={"Bucket": BUCKET, "Key": key,
                "ResponseContentDisposition": "attachment; filename*=UTF-8''" + quote(filename, safe=""),
                "ResponseContentType": "application/octet-stream"},
        ExpiresIn=60,
        HttpMethod="GET",
    )


def delete_confirmed(key: str) -> None:
    client().delete_object(Bucket=BUCKET, Key=key)
    if head(key) is not None:
        raise RuntimeError("Object deletion has not been confirmed")


def abort_multiparts(key: str) -> None:
    # Include any empty multipart created before an interrupted initiation commit.
    pending = []
    for page in client().get_paginator("list_multipart_uploads").paginate(
        Bucket=BUCKET, Prefix=key
    ):
        pending.extend(x for x in page.get("Uploads", []) if x["Key"] == key)
    for pending_upload in pending:
        try:
            client().abort_multipart_upload(
                Bucket=BUCKET, Key=key, UploadId=pending_upload["UploadId"]
            )
        except ClientError as exc:
            if not is_missing(exc):
                raise
    # Confirm abort before releasing the reservation. Complete operations are
    # fenced by the durable session state before this function is called.
    for page in client().get_paginator("list_multipart_uploads").paginate(
        Bucket=BUCKET, Prefix=key
    ):
        if any(x["Key"] == key for x in page.get("Uploads", [])):
            raise RuntimeError("Multipart abort has not been confirmed")


def abort_and_delete(upload: dict) -> None:
    abort_multiparts(upload["object_key"])
    delete_confirmed(upload["object_key"])


def verify_object(upload: dict, heartbeat: Callable[[], None]) -> str | None:
    """Return a rejection code, or None after SHA-256 AND antivirus pass.

    Scanner transport/errors raise, preserving PENDING_SCAN for a bounded retry.
    The stream uses 256 KiB buffers regardless of the object's total size.
    """
    digest = hashlib.sha256()
    size = 0
    deadline = time.monotonic() + 600
    body = client().get_object(Bucket=BUCKET, Key=upload["object_key"])["Body"]
    try:
        with socket.create_connection(
            (os.getenv("CLAMAV_HOST", "antivirus"), int(os.getenv("CLAMAV_PORT", "3310"))),
            timeout=10,
        ) as scanner:
            scanner.settimeout(60)
            scanner.sendall(b"zINSTREAM\0")
            while True:
                if time.monotonic() > deadline:
                    raise ScannerUnavailable("Antivirus scan exceeded its deadline")
                chunk = body.read(CHUNK_SIZE)
                if not chunk:
                    break
                heartbeat()
                size += len(chunk)
                if size > upload["size_bytes"]:
                    return "CHECKSUM_MISMATCH"
                digest.update(chunk)
                scanner.sendall(struct.pack("!I", len(chunk)))
                scanner.sendall(chunk)
            scanner.sendall(struct.pack("!I", 0))
            response = bytearray()
            while not response.endswith(b"\0"):
                heartbeat()
                if time.monotonic() > deadline:
                    raise ScannerUnavailable("Antivirus scan exceeded its deadline")
                try:
                    chunk = scanner.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > 16384:
                    raise ScannerUnavailable("Invalid antivirus response")
        result = bytes(response).rstrip(b"\0\n").decode("utf-8", errors="replace")
        if result.endswith(" FOUND"):
            return "CONTENT_REJECTED"
        if result != "stream: OK":
            raise ScannerUnavailable("Antivirus did not confirm a clean result")
        if size != upload["size_bytes"] or digest.hexdigest() != upload["sha256"].lower():
            return "CHECKSUM_MISMATCH"
        return None
    except (OSError, TimeoutError) as exc:
        raise ScannerUnavailable("Antivirus connection unavailable") from exc
    finally:
        body.close()
