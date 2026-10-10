"""OIDC BFF sessions and transaction-scoped, uncached authorization."""
import base64
import hashlib
import hmac
import json
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from urllib.parse import urlencode, urlsplit

import httpx
import jwt
from cryptography.fernet import Fernet
from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import RedirectResponse
from psycopg.types.json import Jsonb

from .config import settings
from .db import connection
from .errors import Problem

router = APIRouter()
ROLE_ORDER = {'VIEWER':1,'EDITOR':2,'OWNER':3}


def now():
    return datetime.now(timezone.utc)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def cipher():
    if len(settings.app_secret) < 32:
        raise RuntimeError('APP_SECRET must contain at least 32 random characters')
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(settings.app_secret.encode()).digest()))


@dataclass(frozen=True)
class Actor:
    id: UUID
    email: str
    display_name: str
    security_admin: bool
    csrf_token: str


def audit(conn, actor_id, action, target_id, request_id, result='SUCCESS', workspace_id=None, effect_key=None):
    conn.execute('''INSERT INTO audit_events(id,actor_id,action,target_id,workspace_id,result,request_id,effect_key)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(effect_key) DO NOTHING''',
                 (uuid4(),actor_id,action,target_id,workspace_id,result,request_id or uuid4(),effect_key))


def normalize_name(name):
    name = unicodedata.normalize('NFC', name)
    if not name or name in ('.','..') or name != name.strip() or len(name)>255 or any(ord(c)<32 or c in '/\\\x7f' for c in name):
        raise Problem(400,'VALIDATION_FAILED','Names must have 1–255 characters, without control characters, path separators, or surrounding spaces.')
    return name.casefold()


def _key(key):
    try:
        return UUID(str(key))
    except (ValueError,TypeError):
        raise Problem(400,'VALIDATION_FAILED','A UUID Idempotency-Key header is required.')


def request_hash(body):
    return digest(json.dumps(jsonable_encoder(body),sort_keys=True,separators=(',',':')))


def idempotency_get(conn, actor_id, operation, key, body):
    key = _key(key)
    lock = int.from_bytes(hashlib.sha256(f'{actor_id}:{operation}:{key}'.encode()).digest()[:8], 'big',signed=True)
    conn.execute('SELECT pg_advisory_xact_lock(%s)',(lock,))
    row = conn.execute('SELECT * FROM idempotency_records WHERE actor_id=%s AND operation=%s AND key=%s FOR UPDATE',
                       (actor_id,operation,key)).fetchone()
    if not row:
        return None
    if row['expires_at'] <= now():
        conn.execute('DELETE FROM idempotency_records WHERE actor_id=%s AND operation=%s AND key=%s',(actor_id,operation,key))
        return None
    if row['request_hash'] != request_hash(body):
        raise Problem(409,'IDEMPOTENCY_KEY_REUSED','This key was already used with a different request.')
    return json.loads(cipher().decrypt(row['response_encrypted'].encode())) if row['response_encrypted'] else row['response']


def idempotency_put(conn, actor_id, operation, key, body, response, status=200, sensitive=False):
    encoded = jsonable_encoder(response)
    encrypted = cipher().encrypt(json.dumps(encoded).encode()).decode() if sensitive else None
    conn.execute('''INSERT INTO idempotency_records(actor_id,operation,key,request_hash,response,response_encrypted,status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                 (actor_id,operation,_key(key),request_hash(body),None if sensitive else Jsonb(encoded),encrypted,status))


def workspace_role(conn, actor_id, workspace_id):
    row = conn.execute('''SELECT m.role FROM memberships m JOIN principals p ON p.id=m.principal_id
        WHERE m.workspace_id=%s AND (p.user_id=%s OR EXISTS
        (SELECT 1 FROM principal_members pm WHERE pm.group_principal_id=p.id AND pm.user_id=%s))
        ORDER BY CASE m.role WHEN 'OWNER' THEN 3 WHEN 'EDITOR' THEN 2 ELSE 1 END DESC LIMIT 1''',
        (workspace_id,actor_id,actor_id)).fetchone()
    return row['role'] if row else None


def lock_workspace(conn, workspace_id, lock='share'):
    if lock not in ('share','update'):
        raise ValueError('Unsupported lock mode')
    row = conn.execute('SELECT * FROM workspaces WHERE id=%s FOR '+ ('UPDATE' if lock=='update' else 'SHARE'),(workspace_id,)).fetchone()
    if not row:
        raise Problem(404,'NOT_FOUND','Workspace not found.')
    return row


def workspace_access(conn, actor, workspace_id, minimum='VIEWER', lock='share'):
    row = lock_workspace(conn, workspace_id, lock)
    role = workspace_role(conn,actor.id,workspace_id)
    if ROLE_ORDER.get(role,0) < ROLE_ORDER[minimum]:
        raise Problem(403,'ACCESS_DENIED','You do not have permission in this workspace.')
    row['my_role'] = role
    return row


def file_access(conn, actor, file_id, minimum='VIEWER', lock='share', allow_deleted=False):
    loc = conn.execute('SELECT workspace_id FROM files WHERE id=%s',(file_id,)).fetchone()
    if not loc:
        raise Problem(404,'NOT_FOUND','File not found.')
    lock_workspace(conn,loc['workspace_id'],lock)
    row = conn.execute('SELECT * FROM files WHERE id=%s FOR '+('UPDATE' if lock=='update' else 'SHARE'),(file_id,)).fetchone()
    inherited = workspace_role(conn,actor.id,row['workspace_id'])
    personal = conn.execute('SELECT role FROM file_permissions WHERE file_id=%s AND user_id=%s',(file_id,actor.id)).fetchone()
    role = max([inherited,personal['role'] if personal else None],key=lambda x:ROLE_ORDER.get(x,0))
    if ROLE_ORDER.get(role,0) < ROLE_ORDER[minimum]:
        raise Problem(403,'ACCESS_DENIED','You do not have permission for this file.')
    if row['purge_state']=='PURGED':
        raise Problem(404,'NOT_FOUND','File not found.')
    if row['purge_state']=='PURGING':
        raise Problem(409,'FILE_PURGING','Permanent deletion has begun.')
    if not allow_deleted and row['purge_state']!='ACTIVE':
        raise Problem(409,'FILE_TRASHED','Restore this file from trash first.')
    row['my_role'] = role
    return row


def internal_url(url):
    # Only the configured issuer can be translated; discovered foreign endpoints are rejected.
    if url != settings.oidc_issuer and not url.startswith(settings.oidc_issuer+'/'):
        raise Problem(503,'DEPENDENCY_UNAVAILABLE','Identity provider endpoint is outside the configured issuer.')
    return settings.oidc_internal_url + url[len(settings.oidc_issuer):]


def oidc_http(method,url,operation,**kwargs):
    from .observability import dependency_result
    try:
        response=httpx.request(method,url,**kwargs)
    except httpx.HTTPError:
        dependency_result('oidc',operation,'failure')
        raise
    dependency_result('oidc',operation,'failure' if response.status_code>=500 or response.status_code==429 else 'success')
    return response


def oidc_metadata():
    try:
        response = oidc_http('GET',settings.oidc_internal_url+'/.well-known/openid-configuration','discovery',timeout=5)
        response.raise_for_status()
        meta = response.json()
        if meta['issuer'] != settings.oidc_issuer:
            raise ValueError('Issuer mismatch')
        for name in ('authorization_endpoint','token_endpoint','jwks_uri'):
            internal_url(meta[name])
        return meta
    except (httpx.HTTPError,ValueError,KeyError):
        raise Problem(503,'DEPENDENCY_UNAVAILABLE','Identity provider is unavailable.')


def token_request(meta, data):
    try:
        response = oidc_http('POST',internal_url(meta['token_endpoint']),'token',data={**data,'client_id':settings.oidc_client_id,
                             'client_secret':settings.oidc_client_secret},timeout=8)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError,ValueError):
        raise Problem(503,'DEPENDENCY_UNAVAILABLE','Identity provider could not validate the session.')


def verify_token(meta, token, nonce=None):
    try:
        header = jwt.get_unverified_header(token)
        if header.get('alg') != 'RS256':
            raise ValueError('Unexpected signing algorithm')
        response = oidc_http('GET',internal_url(meta['jwks_uri']),'jwks',timeout=5)
        response.raise_for_status()
        keys = response.json()['keys']
        jwk = next(k for k in keys if k['kid']==header['kid'] and k.get('kty')=='RSA')
        claims = jwt.decode(token,jwt.PyJWK.from_dict(jwk).key,algorithms=['RS256'],
                            audience=settings.oidc_client_id,issuer=settings.oidc_issuer,
                            options={'require':['exp','iat','iss','sub','aud']},leeway=10)
        if not claims.get('email_verified') or not isinstance(claims.get('email'),str):
            raise ValueError('Verified corporate email required')
        if nonce is not None and not hmac.compare_digest(claims.get('nonce',''),nonce):
            raise ValueError('Nonce mismatch')
        if isinstance(claims['aud'],list) and len(claims['aud'])>1 and claims.get('azp')!=settings.oidc_client_id:
            raise ValueError('Authorized party mismatch')
        return claims
    except (httpx.HTTPError,ValueError,KeyError,StopIteration,jwt.PyJWTError):
        raise Problem(401,'UNAUTHENTICATED','The identity provider response could not be verified.')


def sync_identity(conn, claims, expected_user=None):
    user = conn.execute('SELECT * FROM users WHERE idp_issuer=%s AND idp_subject=%s FOR UPDATE',
                        (settings.oidc_issuer,claims['sub'])).fetchone()
    if not user and expected_user:
        raise Problem(401,'UNAUTHENTICATED','Corporate identity changed; sign in again.')
    if not user:
        # One-time binding of *local demo* accounts only. Production principals must be provisioned by issuer+sub.
        local_demo = settings.seed_demo and settings.oidc_issuer == settings.app_origin+'/dex' and urlsplit(settings.app_origin).hostname in ('localhost','127.0.0.1')
        if local_demo:
            user = conn.execute('SELECT * FROM users WHERE email=%s AND idp_subject IS NULL FOR UPDATE',(claims['email'].lower(),)).fetchone()
        if user:
            conn.execute('UPDATE users SET idp_issuer=%s,idp_subject=%s WHERE id=%s',(settings.oidc_issuer,claims['sub'],user['id']))
        else:
            user = conn.execute('''INSERT INTO users(id,email,display_name,idp_issuer,idp_subject)
                VALUES (%s,%s,%s,%s,%s) RETURNING *''',(uuid4(),claims['email'].lower(),claims.get('name',claims['email']),settings.oidc_issuer,claims['sub'])).fetchone()
            conn.execute("INSERT INTO principals(id,kind,user_id) VALUES (%s,'USER',%s)",(uuid4(),user['id']))
    if expected_user and user['id'] != expected_user:
        raise Problem(401,'UNAUTHENTICATED','Corporate identity changed; sign in again.')
    if user['status']!='ACTIVE':
        raise Problem(401,'UNAUTHENTICATED','This corporate account has been disabled.')
    groups = claims.get('groups',[])
    if not isinstance(groups,list) or any(not isinstance(g,str) for g in groups):
        raise Problem(401,'UNAUTHENTICATED','Invalid group projection from the identity provider.')
    desired = set()
    for group in groups:
        row = conn.execute('''INSERT INTO principals(id,kind,idp_issuer,idp_group_key) VALUES (%s,'GROUP',%s,%s)
                  ON CONFLICT(idp_issuer,idp_group_key) DO UPDATE SET idp_group_key=excluded.idp_group_key RETURNING id''',
                  (uuid4(),settings.oidc_issuer,group)).fetchone()
        desired.add(row['id'])
    existing = {r['group_principal_id'] for r in conn.execute('''SELECT pm.group_principal_id FROM principal_members pm
               JOIN principals p ON p.id=pm.group_principal_id WHERE pm.user_id=%s AND p.idp_issuer=%s''',(user['id'],settings.oidc_issuer)).fetchall()}
    changed = existing.symmetric_difference(desired)
    if changed:
        affected = conn.execute('SELECT DISTINCT workspace_id FROM memberships WHERE principal_id=ANY(%s) ORDER BY workspace_id',(list(changed),)).fetchall()
        for r in affected:
            conn.execute('UPDATE workspaces SET acl_revision=acl_revision+1 WHERE id=%s',(r['workspace_id'],))
        if existing-desired:
            conn.execute('DELETE FROM principal_members WHERE user_id=%s AND group_principal_id=ANY(%s)',(user['id'],list(existing-desired)))
        for group in desired-existing:
            conn.execute('INSERT INTO principal_members(group_principal_id,user_id) VALUES (%s,%s)',(group,user['id']))
    conn.execute('UPDATE users SET identity_checked_at=now() WHERE id=%s',(user['id'],))
    return user


@router.get('/auth/login')
def login(request: Request):
    meta = oidc_metadata()
    state, nonce, verifier = secrets.token_urlsafe(32),secrets.token_urlsafe(32),secrets.token_urlsafe(48)
    with connection() as conn:
        conn.execute('DELETE FROM oidc_states WHERE expires_at<now()')
        conn.execute('INSERT INTO oidc_states(state_hash,nonce,verifier_encrypted,expires_at) VALUES (%s,%s,%s,%s)',
                     (digest(state),nonce,cipher().encrypt(verifier.encode()).decode(),now()+timedelta(minutes=5)))
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
    response = RedirectResponse(meta['authorization_endpoint']+'?'+urlencode(dict(client_id=settings.oidc_client_id,
        redirect_uri=settings.app_origin+'/auth/callback',response_type='code',scope='openid profile email groups offline_access',
        state=state,nonce=nonce,code_challenge=challenge,code_challenge_method='S256')))
    response.set_cookie('fileflow_oidc_state',state,max_age=300,secure=settings.cookie_secure,httponly=True,samesite='lax',path='/')
    return response


@router.get('/auth/callback')
def callback(request: Request, state: str='', code: str=''):
    browser_state = request.cookies.get('fileflow_oidc_state','')
    if not state or not code or not browser_state or not hmac.compare_digest(browser_state,state):
        raise Problem(401,'UNAUTHENTICATED','The sign-in state is invalid or expired.')
    with connection() as conn:
        flow = conn.execute('DELETE FROM oidc_states WHERE state_hash=%s AND expires_at>now() RETURNING *',(digest(state),)).fetchone()
    if not flow:
        raise Problem(401,'UNAUTHENTICATED','The sign-in state is invalid or expired.')
    meta = oidc_metadata()
    tokens = token_request(meta,dict(grant_type='authorization_code',code=code,redirect_uri=settings.app_origin+'/auth/callback',
                                   code_verifier=cipher().decrypt(flow['verifier_encrypted'].encode()).decode()))
    claims = verify_token(meta,tokens.get('id_token',''),flow['nonce'])
    refresh = tokens.get('refresh_token')
    if not refresh:
        raise Problem(503,'DEPENDENCY_UNAVAILABLE','Identity provider must support offline refresh for session revalidation.')
    session_id, csrf = secrets.token_urlsafe(48),secrets.token_urlsafe(32)
    with connection() as conn:
        user = sync_identity(conn,claims)
        old_session = request.cookies.get(settings.cookie_name)
        if old_session:
            conn.execute('UPDATE sessions SET revoked_at=now() WHERE id_hash=%s',(digest(old_session),))
        conn.execute('''INSERT INTO sessions(id_hash,user_id,csrf_token,refresh_token_encrypted,expires_at)
                       VALUES (%s,%s,%s,%s,%s)''',(digest(session_id),user['id'],csrf,cipher().encrypt(refresh.encode()).decode(),now()+timedelta(hours=8)))
        audit(conn,user['id'],'SESSION_LOGIN',None,request.state.request_id)
    response = RedirectResponse('/',status_code=303)
    response.delete_cookie('fileflow_oidc_state',path='/')
    response.set_cookie(settings.cookie_name,session_id,max_age=8*3600,secure=settings.cookie_secure,httponly=True,samesite='lax',path='/')
    return response


def get_actor(request: Request):
    raw = request.cookies.get(settings.cookie_name)
    if not raw:
        raise Problem(401,'UNAUTHENTICATED','Sign in with your corporate account.')
    with connection() as conn:
        session = conn.execute('''SELECT s.*,u.email,u.display_name,u.security_admin,u.status FROM sessions s
                   JOIN users u ON u.id=s.user_id WHERE s.id_hash=%s FOR UPDATE OF s''',(digest(raw),)).fetchone()
        if not session or session['revoked_at'] or session['expires_at']<=now() or session['last_seen_at']<now()-timedelta(minutes=30) or session['status']!='ACTIVE':
            raise Problem(401,'UNAUTHENTICATED','Your session expired. Please sign in again.')
        if request.method not in ('GET','HEAD','OPTIONS'):
            origin = request.headers.get('origin')
            if origin and origin != settings.app_origin:
                raise Problem(403,'ACCESS_DENIED','Request origin is not allowed.')
            supplied = request.headers.get('X-CSRF-Token','')
            if not supplied:
                raise Problem(400,'BAD_REQUEST','The X-CSRF-Token header is required.')
            if not hmac.compare_digest(supplied,session['csrf_token']):
                raise Problem(403,'ACCESS_DENIED','A valid CSRF token is required.')
        if session['identity_checked_at']<now()-timedelta(seconds=240):
            if not session['refresh_token_encrypted']:
                raise Problem(401,'UNAUTHENTICATED','Corporate identity needs to be verified again.')
            meta = oidc_metadata()
            tokens = token_request(meta,dict(grant_type='refresh_token',refresh_token=cipher().decrypt(session['refresh_token_encrypted'].encode()).decode()))
            claims = verify_token(meta,tokens.get('id_token',''))
            sync_identity(conn,claims,session['user_id'])
            encrypted = cipher().encrypt(tokens['refresh_token'].encode()).decode() if tokens.get('refresh_token') else session['refresh_token_encrypted']
            conn.execute('UPDATE sessions SET identity_checked_at=now(),refresh_token_encrypted=%s WHERE id_hash=%s',(encrypted,digest(raw)))
        conn.execute('UPDATE sessions SET last_seen_at=now() WHERE id_hash=%s',(digest(raw),))
        rate = conn.execute('''INSERT INTO rate_limits(bucket,window_start,hits) VALUES (%s,date_trunc('minute',now()),1)
            ON CONFLICT(bucket) DO UPDATE SET hits=CASE WHEN rate_limits.window_start=date_trunc('minute',now())
             THEN rate_limits.hits+1 ELSE 1 END,window_start=date_trunc('minute',now()) RETURNING hits''',(str(session['user_id']),)).fetchone()['hits']
        actor = Actor(session['user_id'],session['email'],session['display_name'],session['security_admin'],session['csrf_token'])
    if rate>settings.rate_limit:
        raise Problem(429,'RATE_LIMITED','Too many requests. Please try again shortly.')
    request.state.actor_id = actor.id
    return actor
