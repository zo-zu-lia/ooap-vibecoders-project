"""Focused verification of the security boundary around bytes and scanning."""
import hashlib
import io
from unittest.mock import Mock

import pytest

from app import storage


class Scanner:
    def __init__(self, result):
        self.result = result
        self.largest_write = 0
        self.commands = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def settimeout(self, timeout):
        pass

    def sendall(self, data):
        self.largest_write = max(self.largest_write, len(data))
        if len(data) < 20:
            self.commands.append(data)

    def recv(self, size):
        value, self.result = self.result, b""
        return value


@pytest.mark.parametrize("result,expected", [
    (b"stream: OK\0", None),
    (b"stream: Eicar-Test-Signature FOUND\0", "CONTENT_REJECTED"),
])
def test_scan_stream_is_bounded_and_requires_explicit_clean(monkeypatch, result, expected):
    data = b"ordinary text\n" * 100000
    source = io.BytesIO(data)
    scanner = Scanner(result)
    monkeypatch.setattr(storage, "client", lambda: Mock(get_object=lambda **_: {"Body": source}))
    monkeypatch.setattr(storage.socket, "create_connection", lambda *_, **__: scanner)
    heartbeat = Mock()
    upload = dict(object_key="private/test", size_bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    assert storage.verify_object(upload, heartbeat) == expected
    assert scanner.largest_write <= storage.CHUNK_SIZE
    assert scanner.commands[0] == b"zINSTREAM\0"
    assert scanner.commands[-1] == b"\x00\x00\x00\x00"
    assert heartbeat.call_count > 1
    assert source.closed


@pytest.mark.parametrize("reply", [b"", b"stream: timeout ERROR\0", b"stream: something\0", b"OK\0"])
def test_unknown_or_incomplete_scanner_reply_is_never_clean(monkeypatch, reply):
    source = io.BytesIO(b"safe")
    monkeypatch.setattr(storage, "client", lambda: Mock(get_object=lambda **_: {"Body": source}))
    monkeypatch.setattr(storage.socket, "create_connection", lambda *_, **__: Scanner(reply))
    with pytest.raises(storage.ScannerUnavailable):
        storage.verify_object(dict(object_key="x", size_bytes=4, sha256=hashlib.sha256(b"safe").hexdigest()), lambda: None)
    assert source.closed


def test_clean_antivirus_does_not_bypass_checksum(monkeypatch):
    source = io.BytesIO(b"changed")
    monkeypatch.setattr(storage, "client", lambda: Mock(get_object=lambda **_: {"Body": source}))
    monkeypatch.setattr(storage.socket, "create_connection", lambda *_, **__: Scanner(b"stream: OK\0"))
    assert storage.verify_object(dict(object_key="x", size_bytes=7, sha256="0"*64), lambda: None) == "CHECKSUM_MISMATCH"


def test_manifest_checks_real_part_size_and_etag(monkeypatch):
    actual = [{"PartNumber": 1, "ETag": '"one"', "Size": storage.PART_SIZE},
              {"PartNumber": 2, "ETag": '"two"', "Size": 1}]
    s3 = Mock()
    s3.get_paginator.return_value.paginate.return_value = [{"Parts": actual}]
    monkeypatch.setattr(storage, "client", lambda: s3)
    upload = dict(object_key="x", storage_upload_id="id", part_count=2,
                  part_size_bytes=storage.PART_SIZE, size_bytes=storage.PART_SIZE+1)
    parts = [{"partNumber": 1, "etag": '"one"'}, {"partNumber": 2, "etag": '"two"'}]
    storage.validate_parts(upload, parts)
    actual[1]["Size"] = 2
    with pytest.raises(storage.InvalidManifest):
        storage.validate_parts(upload, parts)


def test_manifest_rejects_duplicate_and_out_of_order_parts(monkeypatch):
    s3 = Mock()
    monkeypatch.setattr(storage, "client", lambda: s3)
    for numbers in ([1, 1], [2, 1], [1], [1, 2, 3]):
        with pytest.raises(storage.InvalidManifest):
            storage.validate_parts({"part_count": 2}, [{"partNumber": n, "etag": "x"} for n in numbers])
    s3.get_paginator.assert_not_called()
