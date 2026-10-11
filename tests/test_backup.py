"""Authentication and content verification of streaming recovery archives."""
import importlib.util
import io
import json
from pathlib import Path
import tarfile

from cryptography.exceptions import InvalidTag
import pytest

spec=importlib.util.spec_from_file_location('fileflow_backup',Path(__file__).resolve().parents[1]/'scripts/backup.py')
backup=importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)


def archive(tmp_path,monkeypatch):
    monkeypatch.setattr(backup,'LOCAL',tmp_path)
    path=tmp_path/'test.ffbackup'
    payload=b'bounded recovery content\n'*10000
    with path.open('wb') as out:
        writer=backup.EncryptWriter(out)
        manifest={}
        with tarfile.open(fileobj=writer,mode='w|') as tar:
            backup.add(tar,'objects/0',io.BytesIO(payload),len(payload),manifest)
            data=json.dumps({'files':manifest,'objects':[{'key':'sample','member':'objects/0'}]}).encode()
            info=tarfile.TarInfo('manifest.json'); info.size=len(data)
            tar.addfile(info,io.BytesIO(data))
        writer.finish()
    return path,payload


def test_stream_archive_preserves_content(tmp_path,monkeypatch):
    path,payload=archive(tmp_path,monkeypatch)
    with backup.decrypted(path) as (tar,manifest):
        assert tar.extractfile('objects/0').read()==payload
        assert manifest['files']['objects/0']['bytes']==len(payload)


def test_tampered_archive_is_rejected_before_parsing(tmp_path,monkeypatch):
    path,_=archive(tmp_path,monkeypatch)
    with path.open('r+b') as data:
        data.seek(100); original=data.read(1); data.seek(100)
        data.write(bytes([original[0]^1]))
    with pytest.raises(InvalidTag):
        with backup.decrypted(path):
            pytest.fail('Tampered bytes reached the archive parser')
