import hashlib
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import httpx
import pytest

from conftest import ORIGIN, API, validate_contract

pytestmark = pytest.mark.integration
WORKSPACE = '00000000-0000-4000-8000-000000000010'
ROOT_FOLDER = '00000000-0000-4000-8000-000000000011'
FINANCE = '00000000-0000-4000-8000-000000000020'
FINANCE_USER = '00000000-0000-4000-8000-000000000005'


def test_auth_browse_folders_and_validation(accounts):
    owner, viewer, admin = (accounts[r] for r in ('owner','viewer','security'))
    assert httpx.get(ORIGIN+'/api/v1/me').status_code == 401
    spaces = owner.get('/workspaces')
    assert [s['id'] for s in spaces] == [WORKSPACE]
    assert admin.get('/workspaces') == []
    owner.get(f'/workspaces/{FINANCE}/items', expected=403)
    name = 'Integration '+uuid4().hex[:8]
    body = dict(parentId=ROOT_FOLDER, name=name)
    folder = owner.post(f'/workspaces/{WORKSPACE}/folders', json=body, expected=201)
    owner.post(f'/workspaces/{WORKSPACE}/folders', json=body, expected=409)
    viewer.post(f'/workspaces/{WORKSPACE}/folders', json=dict(name='denied'), expected=403)
    assert folder['id'] in [x['id'] for x in owner.get(f'/workspaces/{WORKSPACE}/items')['items']]
    owner.get('/files/not-a-uuid', expected=400)
    missing_csrf = owner.client.post('/api/v1/workspaces/'+WORKSPACE+'/folders',
        headers={'X-CSRF-Token':''}, json={'name':'must not exist'})
    assert missing_csrf.status_code == 400
    external = owner.client.post('/api/v1/workspaces/'+WORKSPACE+'/folders',
        headers={'Origin':'https://evil.invalid'}, json={'name':'must not exist'})
    assert external.status_code in (400,403)


def test_multipart_download_versions_restore_and_retries(accounts):
    owner = accounts['owner']
    data = b'abcdefgh'*1_100_000  # > 8 MiB exercises actual multipart boundaries.
    session, parts = owner.prepare(data)
    key = str(uuid4())
    first = owner.complete(session, parts, key=key)
    again = owner.complete(session, parts, key=key)
    assert again == first
    ready = owner.wait_scan(session)
    fid = ready['fileId']
    info = owner.get(f'/files/{fid}')
    assert info['latestVersionNo'] == info['downloadableVersionNo'] == 1
    link = owner.get(f'/files/{fid}/download-link')
    downloaded = httpx.get(link['url'], timeout=30)
    assert downloaded.status_code == 200
    assert hashlib.sha256(downloaded.content).digest() == hashlib.sha256(data).digest()
    new = owner.upload(b'Second version', file_id=fid, expected_version=1)
    assert new['versionNo'] == 2
    versions = owner.get(f'/files/{fid}/versions')
    assert len(versions) == 2
    before = owner.get('/workspaces')[0]['usedBytes']
    owner.post(f'/files/{fid}/versions/1/restore', json={'expectedVersionNo':2}, expected=201)
    after = owner.get('/workspaces')[0]['usedBytes']
    assert after == before, 'Restore must not count physical bytes twice'
    assert owner.get(f'/files/{fid}')['latestVersionNo'] == 3
    link = owner.get(f'/files/{fid}/download-link')
    assert httpx.get(link['url']).content == data
    owner.post(f'/files/{fid}/versions/1/restore', json={'expectedVersionNo':2}, expected=409)


def test_checksums_malware_and_previous_clean_version(accounts):
    owner = accounts['owner']
    clean = owner.upload()
    fid = clean['fileId']
    session, parts = owner.prepare(b'corrupted content', file_id=fid, expected_version=1, sha='0'*64)
    owner.complete(session, parts)
    owner.wait_scan(session, status='REJECTED')
    assert owner.get(f'/files/{fid}/download-link')['versionNo'] == 1
    # Standard harmless EICAR antivirus test fixture, never executable code.
    eicar = b'X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*'
    session, parts = owner.prepare(eicar, name=f'eicar-{uuid4().hex}.txt')
    owner.complete(session, parts)
    result = owner.wait_scan(session, status='REJECTED')
    owner.get(f'/files/{result["fileId"]}/download-link', expected=422)


def test_optimistic_concurrency(accounts):
    owner = accounts['owner']
    fid = owner.upload()['fileId']
    a, pa = owner.prepare(b'writer A', file_id=fid, expected_version=1)
    b, pb = owner.prepare(b'writer B', file_id=fid, expected_version=1)
    def commit(pair):
        s,p = pair
        return owner.client.post(f'/api/v1/uploads/{s["uploadId"]}/complete', json=p,
                                 headers={'Idempotency-Key':str(uuid4())})
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(commit, [(a,pa),(b,pb)]))
    assert sorted(r.status_code for r in outcomes) == [202,409]
    assert next(r.json() for r in outcomes if r.status_code==409)['code'] == 'VERSION_CONFLICT'
    assert len(owner.get(f'/files/{fid}/versions')) == 2


def test_permissions_search_and_addressed_sharing(accounts):
    owner, finance, viewer, admin = (accounts[r] for r in ('owner','finance','viewer','security'))
    unique = 'access-'+uuid4().hex
    fid = owner.upload(name=unique+'.txt')['fileId']
    finance.get(f'/files/{fid}', expected=403)
    admin.get(f'/files/{fid}/download-link', expected=403)
    # Other demo files may already be shared with Finance. In that case search
    # is allowed, but it must still hide this newly created, unshared file.
    hidden = finance.client.get('/api/v1/search',params={'workspaceId':WORKSPACE,'q':unique})
    assert hidden.status_code in (200,403)
    validate_contract('GET','/search',hidden)
    if hidden.status_code == 200:
        assert hidden.json()['items'] == []
    owner.request('PUT', f'/files/{fid}/permissions/{FINANCE_USER}', json={'role':'VIEWER'})
    assert finance.get(f'/files/{fid}/download-link')['versionNo'] == 1
    assert fid in [x['id'] for x in finance.get('/search', params={'workspaceId':WORKSPACE,'q':unique})['items']]
    assert owner.get(f'/files/{fid}/permissions')
    owner.request('DELETE', f'/files/{fid}/permissions/{FINANCE_USER}', expected=204)
    finance.get(f'/files/{fid}/download-link', expected=403)
    key = str(uuid4())
    body = {'recipientUserId':FINANCE_USER, 'expiresInHours':1}
    share = owner.post(f'/files/{fid}/share-links', json=body, expected=201, key=key)
    repeat = owner.post(f'/files/{fid}/share-links', json=body, expected=201, key=key)
    assert repeat == share
    token = share['url'].split('#share=',1)[1]
    result = finance.post('/share-links/resolve', json={'token':token})
    assert result['fileId'] == fid
    viewer.post('/share-links/resolve', json={'token':token}, expected=403)
    assert owner.get(f'/files/{fid}/share-links')[0]['id'] == share['id']
    owner.request('DELETE', f'/share-links/{share["id"]}', expected=204)
    finance.post('/share-links/resolve', json={'token':token}, expected=404)


def test_trash_recover_hold_policy_and_audit(accounts):
    owner, admin, viewer = (accounts[r] for r in ('owner','security','viewer'))
    fid = owner.upload()['fileId']
    owner.request('PATCH', f'/files/{fid}/legal-hold', json={'legalHold':True}, expected=403)
    admin.request('PATCH', f'/files/{fid}/legal-hold', json={'legalHold':True})
    owner.request('DELETE', f'/files/{fid}', expected=204)
    assert fid in [x['id'] for x in owner.get(f'/workspaces/{WORKSPACE}/trash')]
    owner.get(f'/files/{fid}/download-link', expected=409)
    owner.post(f'/files/{fid}/recover')
    assert owner.get(f'/files/{fid}')['legalHold'] is True
    admin.request('PATCH', f'/files/{fid}/legal-hold', json={'legalHold':False})
    policy = admin.get(f'/workspaces/{WORKSPACE}/policy')
    viewer.request('PUT', f'/workspaces/{WORKSPACE}/policy', json=policy, expected=403)
    admin.request('PUT', f'/workspaces/{WORKSPACE}/policy', json=policy)
    events = admin.get('/audit-events')['items']
    assert events and all(x['requestId'] for x in events)
    owner.get('/audit-events', expected=403)


def test_upload_limits_and_idempotency_mismatch(accounts):
    owner, viewer = accounts['owner'], accounts['viewer']
    route = f'/folders/{ROOT_FOLDER}/uploads'
    body = {'name':'reserved-'+uuid4().hex, 'sizeBytes':1, 'sha256':hashlib.sha256(b'x').hexdigest(), 'contentType':'text/plain'}
    viewer.post(route, json=body, expected=403)
    owner.post(route, json={**body, 'sizeBytes':2147483646}, expected=400)
    owner.post(route, json={**body, 'sizeBytes':2147483648}, expected=400)
    key = str(uuid4())
    a = owner.post(route, json=body, key=key, expected=201)
    assert owner.post(route, json=body, key=key, expected=201) == a
    error = owner.post(route, json={**body,'name':'changed'}, key=key, expected=409)
    assert error['code'] == 'IDEMPOTENCY_KEY_REUSED'
    # Finish the fixture so repeated suite runs do not consume active slots.
    signed = owner.get(f'/uploads/{a["uploadId"]}/parts/1/url')
    stored = httpx.put(signed['url'],content=b'x',headers=signed['requiredHeaders'])
    assert stored.status_code == 200
    owner.complete(a,{'parts':[{'partNumber':1,'etag':stored.headers['etag']}]})
    owner.wait_scan(a)


def test_logout_revokes_server_session():
    api=API('viewer')
    try:
        cookie=api.client.cookies.get('fileflow_session')
        api.post('/logout',expected=204)
        assert api.client.get('/api/v1/me').status_code==401
        # Even a stolen old cookie cannot revive the revoked server session.
        assert httpx.get(ORIGIN+'/api/v1/me',cookies={'fileflow_session':cookie}).status_code==401
    finally:
        api.client.close()
