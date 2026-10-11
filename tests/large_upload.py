"""Opt-in scanner-boundary smoke test with bounded memory and real antivirus.

Run after normal integration tests, with the worker running:
    .venv/bin/python tests/large_upload.py

Uses 8 MiB buffers, honors 429 Retry-After, streams the download, records Docker
memory samples, and removes only its own fixture through trash and expiry.

The default is 2 GiB minus 3 bytes, ClamAV 1.5.4's hard INT_MAX-2 limit.
The original exact 2 GiB trial was quarantined by the real scanner; accepting
larger declarations would consume transfer and quota for content it cannot scan.
"""
import argparse
import hashlib
import json
import resource
import subprocess
import threading
import time
from datetime import datetime, timezone
from uuid import uuid4

import httpx

from conftest import API, ROOT, validate_contract
from test_worker_integration import db


class MemorySampler:
    def __init__(self):
        check = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.command = (["docker"] if check.returncode == 0 else ["sudo", "-n", "docker"])
        self.stop = threading.Event()
        self.peak = {}
        self.samples = 0
        self.peak_total = 0
        self.thread = threading.Thread(target=self.run, daemon=True)

    @staticmethod
    def bytes(value):
        for unit, scale in (("GiB", 1024**3), ("MiB", 1024**2), ("KiB", 1024), ("GB", 1000**3), ("MB", 1000**2), ("kB", 1000), ("B", 1)):
            if value.endswith(unit):
                return int(float(value[:-len(unit)]) * scale)
        return 0

    def run(self):
        while not self.stop.is_set():
            try:
                result = subprocess.run(self.command + ["stats", "--no-stream", "--format", "{{json .}}"],
                                        capture_output=True, text=True, timeout=15)
                total = 0
                for line in result.stdout.splitlines():
                    sample = json.loads(line)
                    name = sample["Name"]
                    if name.startswith("fileflow-"):
                        used = self.bytes(sample["MemUsage"].split(" / ")[0])
                        self.peak[name] = max(self.peak.get(name, 0), used)
                        total += used
                self.peak_total = max(self.peak_total, total)
                self.samples += 1
            except (ValueError, KeyError, subprocess.TimeoutExpired):
                pass
            self.stop.wait(2)

    def finish(self):
        self.stop.set()
        self.thread.join(timeout=20)
        return {"samples": self.samples, "peak_container_bytes": self.peak, "peak_total_container_bytes": self.peak_total}


def request(api, method, path, *, expected=200, body=None):
    headers = {"Idempotency-Key": str(uuid4())} if method == "POST" else {}
    for _ in range(12):
        response = api.client.request(method, "/api/v1" + path, json=body, headers=headers)
        if response.status_code == 429:
            delay = min(60, max(1, int(response.headers.get("Retry-After", "30"))))
            print(json.dumps({"phase": "rate_limit_backoff", "seconds": delay}), flush=True)
            time.sleep(delay)
            continue
        validate_contract(method, path, response)
        if response.status_code != expected:
            raise RuntimeError(f"{method} {path}: HTTP {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else None
    raise RuntimeError("Rate limit did not recover after bounded retries")


def cleanup(api, session):
    row = db("SELECT * FROM upload_sessions WHERE id=%s", (session["uploadId"],))[0]
    file_id = str(row["file_id"]) if row["file_id"] else None
    if file_id:
        request(api, "DELETE", f"/files/{file_id}", expected=204)
        db("UPDATE files SET purge_at=now()-interval '1 second' WHERE id=%s RETURNING id", (file_id,))
        query = "SELECT purge_state='PURGED' AS cleaned FROM files WHERE id=%s"
        parameters = (file_id,)
    else:
        db("UPDATE upload_sessions SET expires_at=now()-interval '1 second' WHERE id=%s RETURNING id", (session["uploadId"],))
        query = "SELECT reserved_bytes=0 AS cleaned FROM upload_sessions WHERE id=%s"
        parameters = (session["uploadId"],)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if db(query, parameters)[0]["cleaned"]:
            return True
        time.sleep(3)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=2147483645)
    args = parser.parse_args()
    if not 1 <= args.size <= 2147483648:
        parser.error("Size must be between 1 byte and 2 GiB")
    api = API("owner")
    pattern = b"FileFlow bounded memory upload validation.\n"
    block = pattern * (8 * 1024 * 1024 // len(pattern) + 1)
    block = block[:8 * 1024 * 1024]
    digest = hashlib.sha256()
    for offset in range(0, args.size, len(block)):
        digest.update(block[:min(len(block), args.size-offset)])
    expected_hash = digest.hexdigest()
    report = {"size_bytes": args.size, "sha256": expected_hash,
              "started_at": datetime.now(timezone.utc).isoformat(), "passed": False}
    sampler = MemorySampler()
    sampler.thread.start()
    session = None
    started = time.monotonic()
    try:
        session = request(api, "POST", "/folders/00000000-0000-4000-8000-000000000011/uploads", expected=201,
                          body={"name": f"boundary-test-{uuid4().hex}.txt", "sizeBytes": args.size,
                                "sha256": expected_hash, "contentType": "text/plain"})
        parts = []
        with httpx.Client(timeout=90) as transfer:
            for number in range(1, session["partCount"] + 1):
                signed = request(api, "GET", f"/uploads/{session['uploadId']}/parts/{number}/url")
                part_size = min(session["partSizeBytes"], args.size - (number-1)*session["partSizeBytes"])
                if part_size > len(block):
                    raise RuntimeError("Server increased the part size beyond the fixed test buffer")
                response = transfer.put(signed["url"], content=block[:part_size], headers=signed["requiredHeaders"])
                if response.status_code != 200:
                    raise RuntimeError(f"Part {number} failed with HTTP {response.status_code}")
                parts.append({"partNumber": number, "etag": response.headers["etag"]})
                if number % 32 == 0:
                    print(json.dumps({"phase": "upload", "parts": number, "of": session["partCount"]}), flush=True)
        report["upload_seconds"] = round(time.monotonic() - started, 2)
        request(api, "POST", f"/uploads/{session['uploadId']}/complete", expected=202, body={"parts": parts})
        deadline = time.monotonic() + 720
        state = {}
        while time.monotonic() < deadline:
            state = request(api, "GET", f"/uploads/{session['uploadId']}")
            if state["status"] in {"READY", "REJECTED", "CONFLICT", "EXPIRED"}:
                break
            time.sleep(5)
        report["scan_result"] = state
        if state.get("status") != "READY":
            raise RuntimeError(f"Boundary file failed readiness: {state.get('status')} {state.get('errorCode')}")
        signed = request(api, "GET", f"/files/{state['fileId']}/download-link")
        downloaded = hashlib.sha256()
        count = 0
        with httpx.stream("GET", signed["url"], timeout=90) as response:
            if response.status_code != 200:
                raise RuntimeError(f"Download failed with HTTP {response.status_code}")
            for chunk in response.iter_bytes(256 * 1024):
                downloaded.update(chunk)
                count += len(chunk)
        report["downloaded_bytes"] = count
        report["download_sha256"] = downloaded.hexdigest()
        report["passed"] = count == args.size and downloaded.hexdigest() == expected_hash
        if not report["passed"]:
            raise RuntimeError("Downloaded byte length or SHA-256 differs")
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        if session:
            try:
                report["fixture_purged"] = cleanup(api, session)
            except Exception as exc:
                report["cleanup_error"] = str(exc)
        report["memory"] = sampler.finish()
        report["client_peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        report["elapsed_seconds"] = round(time.monotonic() - started, 2)
        api.client.close()
        destination = ROOT / ".local/large-upload-report.json"
        destination.parent.mkdir(exist_ok=True)
        destination.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2), flush=True)
    return 0 if report["passed"] and report.get("fixture_purged") else 1


if __name__ == "__main__":
    raise SystemExit(main())
