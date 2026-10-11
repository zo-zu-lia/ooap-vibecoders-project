"""Mature cohort arithmetic, privacy boundaries, and event deduplication."""
import json
import subprocess
from uuid import uuid4

import pytest

from conftest import ROOT

pytestmark=pytest.mark.integration
WORKSPACE='00000000-0000-4000-8000-000000000010'
FINANCE='00000000-0000-4000-8000-000000000020'


def test_analytics_access_and_incomplete_observation_windows(accounts):
    owner,viewer,admin=(accounts[r] for r in ('owner','viewer','security'))
    viewer.get('/analytics',params={'workspaceId':WORKSPACE},expected=403)
    owner.get('/analytics',params={'workspaceId':FINANCE},expected=403)
    owner.get('/analytics',expected=403)
    summary=admin.get('/analytics',params={'days':7})
    assert [m['id'] for m in summary['metrics']]==[f'B{i:02d}' for i in range(1,13)]
    assert summary['quality']['isSynthetic'] is True
    assert summary['quality']['notes']
    assert summary['period']['timezone']=='Europe/Kyiv'
    assert set(summary['funnels'])=={'upload','sharing'}
    assert all(m['value'] is None or isinstance(m['value'],(int,float)) for m in summary['metrics'])


def test_mature_synthetic_cohorts_and_replay_deduplication(accounts):
    # Entire fixture rolls back. No fictional storage bytes are published.
    source='''
import json
from datetime import datetime,timedelta,timezone
from uuid import UUID,uuid4
from app.db import connection
from app.analytics import record_event,summarize
from app.security import audit
owner=UUID('00000000-0000-4000-8000-000000000001')
recipient=UUID('00000000-0000-4000-8000-000000000005')
started=datetime.now(timezone.utc)-timedelta(days=3)
with connection() as conn:
    wid,folder,file=uuid4(),uuid4(),uuid4()
    conn.execute('INSERT INTO workspaces(id,name,quota_bytes) VALUES (%s,%s,100000)',(wid,'analytics fixture'))
    conn.execute("INSERT INTO folders(id,workspace_id,name,normalized_name) VALUES (%s,%s,'','')",(folder,wid))
    conn.execute("INSERT INTO files(id,workspace_id,folder_id,name,normalized_name,created_by,latest_version_no) VALUES (%s,%s,%s,'fixture','fixture',%s,8)",(file,wid,folder,owner))
    for index in range(10):
        upload,object=uuid4(),uuid4()
        conn.execute("""INSERT INTO upload_sessions(id,workspace_id,folder_id,file_id,actor_id,name,normalized_name,object_key,size_bytes,sha256,
            content_type,part_size_bytes,part_count,state,created_at,expires_at,version_no)
            VALUES (%s,%s,%s,%s,%s,'fixture','fixture',%s,1,%s,'text/plain',8388608,1,%s,%s,%s,%s)""",
            (upload,wid,folder,file,owner,str(upload),'a'*64,'READY' if index<6 else 'PROCESSING' if index<8 else 'EXPIRED',started,started+timedelta(days=1),index+1 if index<8 else None))
        if index<8:
            conn.execute('INSERT INTO storage_objects(id,workspace_id,object_key,size_bytes,sha256) VALUES (%s,%s,%s,1,%s)',(object,wid,str(upload),'a'*64))
            conn.execute('INSERT INTO file_versions(file_id,version_no,object_id,scan_status,created_by,created_at,upload_id) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                (file,index+1,object,'CLEAN' if index<6 else 'PENDING_SCAN',owner,started+timedelta(minutes=2),upload))
        if index<6:
            conn.execute("INSERT INTO audit_events(id,actor_id,action,target_id,workspace_id,result,request_id,created_at,effect_key) VALUES (%s,NULL,'SCAN_CLEAN',%s,%s,'SUCCESS',%s,%s,%s)",
                (uuid4(),file,wid,uuid4(),started+timedelta(minutes=3),'scan:'+str(upload)))
        if index<3:
            for retry in range(10):
                record_event(conn,event_name='download.link_issued',actor_id=owner,workspace_id=wid,resource_id=file,
                    version_no=index+1,operation_id='fixture-download-'+str(index),occurred_at=started+timedelta(minutes=4))
    for index in range(5):
        link=uuid4()
        conn.execute('INSERT INTO share_links(id,file_id,recipient_user_id,created_by,secret_hash,created_at,expires_at,revoked_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)',
            (link,file,recipient,owner,str(uuid4()),started,started+timedelta(hours=24),started+timedelta(hours=2) if index==4 else None))
        if index<4:
            record_event(conn,event_name='share.resolve_attempted',actor_id=recipient,workspace_id=wid,resource_id=link,
                operation_id=uuid4(),occurred_at=started+timedelta(hours=1))
        if index<3:
            record_event(conn,event_name='share.resolve_succeeded',actor_id=recipient,workspace_id=wid,resource_id=link,
                operation_id=uuid4(),occurred_at=started+timedelta(hours=1))
    result=summarize(conn,started-timedelta(minutes=1),started+timedelta(days=1),wid)
    result['downloadObservations']=conn.execute("SELECT count(*) AS n FROM analytics_events WHERE workspace_id=%s AND event_name='download.link_issued'",(wid,)).fetchone()['n']
    conn.rollback()
print(json.dumps(result))
'''
    response=subprocess.run([str(ROOT/'scripts/compose.sh'),'exec','-T','api','python','-c',source],
                             text=True,capture_output=True,timeout=30)
    assert response.returncode==0,response.stderr
    result=json.loads(response.stdout)
    metrics={m['id']:m for m in result['metrics']}
    assert metrics['B03']['value']==80
    assert metrics['B04']['value']==60
    assert metrics['B09']['value']==60
    assert result['funnels']['upload']==dict(initiated=10,committed=8,ready=6,linkIssued=3,mature=10,provisional=0)
    assert result['funnels']['sharing']==dict(created=5,attempted=4,resolved=3,mature=5,provisional=0)
    assert result['downloadObservations']==3,'Ten replays must count as one issuance event'
