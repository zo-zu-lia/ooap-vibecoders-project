#!/usr/bin/env python3
"""Encrypted, consistent local recovery sets and an isolated restore drill.

Run with .venv/bin/python scripts/backup.py create|verify|drill.
The drill never overwrites the live database or bucket.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import tarfile
import tempfile
import time
from uuid import uuid4

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = [str(ROOT/'scripts/compose.sh')]
LOCAL = ROOT/'.local'
CHUNK = 1024*1024
MAGIC = b'FILEFLOW-BACKUP-1\n'


def command(args, **kwargs):
    return subprocess.run(COMPOSE+args, check=True, **kwargs)


def resume_services(services):
    if not services: return
    command(['start',*services],stdout=subprocess.DEVNULL)
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        output=command(['ps','--format','json',*services],capture_output=True).stdout.decode()
        states=[json.loads(line) for line in output.splitlines() if line.strip()]
        healthy={state['Service'] for state in states if state.get('Health')=='healthy'}
        if set(services)<=healthy: return
        time.sleep(1)
    raise RuntimeError('Application health did not recover after backup maintenance')


def env():
    return dict(line.split('=',1) for line in (ROOT/'.env').read_text().splitlines() if line and not line.startswith('#'))


def s3():
    config=env()
    return boto3.client('s3',endpoint_url=config['S3_PUBLIC_ENDPOINT'],
        aws_access_key_id=config['S3_ACCESS_KEY'],aws_secret_access_key=config['S3_SECRET_KEY'],
        region_name=config['S3_REGION'],config=Config(signature_version='s3v4',s3={'addressing_style':'path'}))


def sql(query, database='fileflow'):
    return command(['exec','-T','postgres','psql','-U','fileflow','-d',database,'-At','-v','ON_ERROR_STOP=1','-c',query],capture_output=True).stdout


def key():
    path=LOCAL/'backup.key'
    if not path.exists():
        path.write_bytes(secrets.token_bytes(32))
        path.chmod(0o600)
    return path.read_bytes()


class EncryptWriter:
    def __init__(self, output):
        nonce=secrets.token_bytes(12)
        self.out=output
        self.out.write(MAGIC+nonce)
        self.encrypt=Cipher(algorithms.AES(key()),modes.GCM(nonce)).encryptor()
        self.encrypt.authenticate_additional_data(MAGIC)

    def write(self, data):
        self.out.write(self.encrypt.update(data))
        return len(data)

    def finish(self):
        self.out.write(self.encrypt.finalize())
        self.out.write(self.encrypt.tag)


class HashReader:
    def __init__(self, source):
        self.source=source
        self.digest=hashlib.sha256()

    def read(self,n=-1):
        result=self.source.read(n)
        self.digest.update(result)
        return result


def add(tar,name,source,size,manifest):
    reader=HashReader(source)
    info=tarfile.TarInfo(name)
    info.size=size
    info.mode=0o600
    tar.addfile(info,reader)
    manifest[name]={'bytes':size,'sha256':reader.digest.hexdigest()}


def create():
    started=time.monotonic()
    folder=ROOT/'backups'
    folder.mkdir(exist_ok=True,mode=0o700)
    snapshot=datetime.now(timezone.utc).isoformat()
    name=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    partial=folder/(name+'.partial')
    destination=folder/(name+'.ffbackup')
    running=command(['ps','--status','running','--services'],capture_output=True).stdout.decode().split()
    resume=[x for x in ('api','worker') if x in running]
    try:
        # Brief maintenance window gives an exact DB/object snapshot, including
        # pending operations. Signed PUTs cannot create a committed object alone.
        if resume: command(['stop',*resume],stdout=subprocess.DEVNULL)
        with tempfile.TemporaryDirectory(dir=LOCAL) as scratch:
            dump=Path(scratch)/'database.dump'
            with dump.open('wb') as out:
                command(['exec','-T','postgres','pg_dump','-U','fileflow','-d','fileflow','-Fc'],stdout=out)
            client=s3()
            bucket=env()['S3_BUCKET']
            manifest={}
            objects=[]
            with partial.open('wb') as output:
                writer=EncryptWriter(output)
                with tarfile.open(fileobj=writer,mode='w|') as tar:
                    with dump.open('rb') as source: add(tar,'database.dump',source,dump.stat().st_size,manifest)
                    for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket):
                        for obj in page.get('Contents',[]):
                            member='objects/'+str(len(objects))
                            stored=client.get_object(Bucket=bucket,Key=obj['Key'])
                            body=stored['Body']
                            try: add(tar,member,body,obj['Size'],manifest)
                            finally: body.close()
                            objects.append({'key':obj['Key'],'member':member,
                                            'contentType':stored.get('ContentType','application/octet-stream'),
                                            'metadata':stored.get('Metadata',{})})
                    metadata={'snapshotAt':snapshot,'objects':objects,'files':manifest,'format':1}
                    data=json.dumps(metadata).encode()
                    info=tarfile.TarInfo('manifest.json'); info.size=len(data); info.mode=0o600
                    tar.addfile(info,io.BytesIO(data))
                writer.finish()
            partial.rename(destination)
    finally:
        resume_services(resume)
        partial.unlink(missing_ok=True)
    verify(destination)
    (LOCAL/'backup-status.json').write_text(json.dumps({'snapshotAt':snapshot,'verifiedAt':datetime.now(timezone.utc).isoformat(),'archive':str(destination),'seconds':time.monotonic()-started}))
    metrics=LOCAL/'metrics'
    metrics.mkdir(exist_ok=True)
    metrics.chmod(0o755)
    output=metrics/'fileflow_backup.prom'
    staging=metrics/'fileflow_backup.tmp'
    staging.write_text('# TYPE fileflow_last_verified_recovery_point_timestamp_seconds gauge\n'
                       f'fileflow_last_verified_recovery_point_timestamp_seconds {datetime.fromisoformat(snapshot).timestamp()}\n')
    staging.chmod(0o644)
    staging.replace(output)
    cutoff=time.time()-30*24*3600
    for old in folder.glob('*.ffbackup'):
        if old!=destination and old.stat().st_mtime<cutoff:
            old.unlink()
    print(f'Created and verified {destination}; encryption key: .local/backup.key')
    return destination


@contextmanager
def decrypted(path):
    if not (LOCAL/'backup.key').is_file():
        raise RuntimeError('Missing original .local/backup.key; refusing to generate a replacement')
    with tempfile.TemporaryDirectory(dir=LOCAL) as scratch:
        target=Path(scratch)/'archive.tar'
        with path.open('rb') as source,target.open('wb') as dest:
            if source.read(len(MAGIC))!=MAGIC: raise ValueError('Not a FileFlow backup')
            nonce=source.read(12)
            source.seek(-16,2); tag=source.read(16)
            remaining=source.tell()-len(MAGIC)-12-16
            source.seek(len(MAGIC)+12)
            decrypt=Cipher(algorithms.AES(key()),modes.GCM(nonce,tag)).decryptor()
            decrypt.authenticate_additional_data(MAGIC)
            while remaining:
                part=source.read(min(CHUNK,remaining))
                if not part: raise ValueError('Truncated backup')
                remaining-=len(part); dest.write(decrypt.update(part))
            dest.write(decrypt.finalize())  # Authenticity checked before parsing.
        with tarfile.open(target,'r:') as tar:
            manifest=json.load(tar.extractfile('manifest.json'))
            for name,expected in manifest['files'].items():
                reader=tar.extractfile(name)
                digest=hashlib.sha256(); size=0
                while part:=reader.read(CHUNK): digest.update(part); size+=len(part)
                if digest.hexdigest()!=expected['sha256'] or size!=expected['bytes']:
                    raise ValueError('Recovery set checksum mismatch')
            yield tar,manifest


def verify(path):
    with decrypted(path) as (_,manifest):
        print(f'Authenticated database archive and {len(manifest["objects"])} stored objects')


def drill(path):
    started=time.monotonic()
    suffix=uuid4().hex[:12]
    database='fileflow_drill_'+suffix
    bucket='fileflow-drill-'+suffix
    client=s3()
    created_db=created_bucket=False
    try:
        with decrypted(path) as (tar,manifest):
            command(['exec','-T','postgres','createdb','-U','fileflow',database]); created_db=True
            client.create_bucket(Bucket=bucket); created_bucket=True
            with tempfile.TemporaryDirectory(dir=LOCAL) as scratch:
                dump=Path(scratch)/'database.dump'
                with dump.open('wb') as dest:
                    source=tar.extractfile('database.dump')
                    while part:=source.read(CHUNK): dest.write(part)
                with dump.open('rb') as source:
                    command(['exec','-T','postgres','pg_restore','-U','fileflow','-d',database,'--no-owner','--no-acl','--exit-on-error'],stdin=source)
            # A recovered session must never become active. This drill stays
            # isolated; promoting a recovery additionally requires current ACLs.
            sql('UPDATE sessions SET revoked_at=now()',database)
            for obj in manifest['objects']:
                reader=tar.extractfile(obj['member'])
                client.upload_fileobj(reader,bucket,obj['key'],
                    ExtraArgs={'ContentType':obj.get('contentType','application/octet-stream'),'Metadata':obj.get('metadata',{})},
                    Config=TransferConfig(max_concurrency=1,use_threads=False))
                body=client.get_object(Bucket=bucket,Key=obj['key'])['Body']
                digest=hashlib.sha256()
                try:
                    while part:=body.read(CHUNK): digest.update(part)
                finally: body.close()
                assert digest.hexdigest()==manifest['files'][obj['member']]['sha256']
            references=json.loads(sql("SELECT COALESCE(json_agg(object_key),'[]'::json) FROM storage_objects WHERE state <> 'DELETED'",database))
            for reference in references: client.head_object(Bucket=bucket,Key=reference)
            # Every CLEAN version must agree with its stored content hash.
            clean=json.loads(sql("SELECT COALESCE(json_agg(DISTINCT jsonb_build_object('key',o.object_key,'sha',o.sha256)),'[]'::json) FROM storage_objects o JOIN file_versions v ON v.object_id=o.id WHERE v.scan_status='CLEAN' AND o.state <> 'DELETED'",database))
            restored_hashes={o['key']:manifest['files'][o['member']]['sha256'] for o in manifest['objects']}
            assert all(restored_hashes[obj['key']]==obj['sha'] for obj in clean)
            files=int(sql("SELECT count(*) FROM files WHERE purge_state='ACTIVE'",database))
            result={'passed':True,'snapshotAt':manifest['snapshotAt'],'objects':len(manifest['objects']),'activeFiles':files,'seconds':round(time.monotonic()-started,2)}
            (LOCAL/'restore-drill.json').write_text(json.dumps(result,indent=2))
            print(json.dumps(result))
    finally:
        if created_bucket:
            for page in client.get_paginator('list_objects_v2').paginate(Bucket=bucket):
                for obj in page.get('Contents',[]): client.delete_object(Bucket=bucket,Key=obj['Key'])
            client.delete_bucket(Bucket=bucket)
        if created_db: command(['exec','-T','postgres','dropdb','-U','fileflow',database])


if __name__=='__main__':
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['create','verify','drill'])
    p.add_argument('archive',type=Path,nargs='?')
    args=p.parse_args()
    if args.action=='create':
        # A scheduled run and a manual run must not overlap maintenance windows.
        with (LOCAL/'backup.lock').open('a') as lock:
            try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise SystemExit('A FileFlow backup is already running.')
            create()
    else:
        if not args.archive: p.error('archive is required for verify/drill')
        (verify if args.action=='verify' else drill)(args.archive.resolve())
