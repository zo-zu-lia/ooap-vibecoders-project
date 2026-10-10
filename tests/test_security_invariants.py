"""Security invariants checked against the running Docker deployment and real OIDC."""
import json
import subprocess
from uuid import UUID, uuid4

import pytest

from conftest import API, ORIGIN, ROOT

pytestmark = pytest.mark.integration
WORKSPACE = '00000000-0000-4000-8000-000000000010'
EDITOR = '00000000-0000-4000-8000-000000000002'
FINANCE = '00000000-0000-4000-8000-000000000005'


def database_check(source):
    """Run bounded checks inside the private service network; never publish PostgreSQL."""
    result = subprocess.run([str(ROOT/'scripts/compose.sh'),'exec','-T','api','python','-c',source],
                            check=True,text=True,capture_output=True,timeout=30)
    return json.loads(result.stdout.strip()) if result.stdout.strip() else None


def test_demo_seed_does_not_restore_removed_membership(accounts):
    result=database_check(f'''
import json
from app.db import connection, seed
with connection() as conn:
    assert conn.execute("SELECT 1 FROM app_metadata WHERE key='local_demo_seed'").fetchone()
    conn.execute("DELETE FROM memberships WHERE workspace_id=%s AND principal_id=%s",('{WORKSPACE}','{EDITOR}'))
    seed(conn)
    remains=conn.execute("SELECT 1 FROM memberships WHERE workspace_id=%s AND principal_id=%s",('{WORKSPACE}','{EDITOR}')).fetchone()
    conn.rollback()
print(json.dumps({{'regranted':bool(remains)}}))
''')
    assert result['regranted'] is False


def test_audit_cannot_be_updated_or_deleted(accounts):
    result=database_check('''
import json
import psycopg
from app.db import connection
blocked=[]
with connection() as conn:
    row=conn.execute('SELECT id FROM audit_events LIMIT 1').fetchone()
    assert row
    for operation in ['UPDATE audit_events SET result=result WHERE id=%s','DELETE FROM audit_events WHERE id=%s']:
        try:
            with conn.transaction():
                conn.execute(operation,(row['id'],))
        except psycopg.errors.RaiseException:
            blocked.append(True)
    conn.rollback()
print(json.dumps({'blocked':blocked}))
''')
    assert result['blocked']==[True,True]


def test_oidc_refresh_and_identity_failure_are_fail_closed(accounts):
    api=API('viewer')
    try:
        cookie=api.client.cookies.get('fileflow_session')
        assert cookie
        # Obtain the hash locally. The plaintext cookie never enters SQL or output.
        import hashlib
        key=hashlib.sha256(cookie.encode()).hexdigest()
        database_check(f'''
from app.db import connection
with connection() as conn:
    conn.execute("UPDATE sessions SET identity_checked_at=now()-interval '10 minutes' WHERE id_hash=%s",('{key}',))
''')
        assert api.get('/me')['id']==api.me['id'], 'Real Dex refresh must revalidate the same subject'
        database_check(f'''
from app.db import connection
from app.security import cipher
with connection() as conn:
    conn.execute("UPDATE sessions SET identity_checked_at=now()-interval '10 minutes',refresh_token_encrypted=%s WHERE id_hash=%s",
                 (cipher().encrypt(b'nonexistent-refresh-token').decode(),'{key}'))
''')
        api.get('/workspaces',expected=503)
    finally:
        api.client.close()


def test_share_secret_only_appears_in_encrypted_replay_cache(accounts):
    owner=accounts['owner']
    file_id=owner.upload(name='encrypted-share-'+uuid4().hex+'.txt')['fileId']
    idem=str(uuid4())
    share=owner.post(f'/files/{file_id}/share-links',json={'recipientUserId':FINANCE,'expiresInHours':1},key=idem,expected=201)
    token=share['url'].split('#share=',1)[1]
    # The API returns the secret once and replays it from encrypted storage only.
    result=database_check(f'''
import json
from app.db import connection
with connection() as conn:
    share=conn.execute('SELECT secret_hash FROM share_links WHERE id=%s',('{UUID(share['id'])}',)).fetchone()
    cached=conn.execute('SELECT response,response_encrypted FROM idempotency_records WHERE key=%s',('{UUID(idem)}',)).fetchone()
print(json.dumps({{'hash':share['secret_hash'],'response':cached['response'],'encrypted':bool(cached['response_encrypted'])}}))
''')
    import hashlib
    assert result['hash']==hashlib.sha256(token.encode()).hexdigest()
    assert result['response'] is None and result['encrypted']
    owner.request('DELETE',f'/share-links/{share["id"]}',expected=204)
    owner.post(f'/files/{file_id}/share-links',json={'recipientUserId':FINANCE,'expiresInHours':1},key=idem,expected=409)
