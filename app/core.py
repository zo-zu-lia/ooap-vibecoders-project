"""Metadata, policy, sharing and audit API. All mutations are transactional."""
import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from .config import settings
from .db import connection
from .errors import Problem
from .security import (Actor, audit, digest, file_access, get_actor, idempotency_get, idempotency_put,
                       lock_workspace, normalize_name, now, workspace_access, workspace_role)

router = APIRouter(prefix='/api/v1')

class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')

class FolderInput(Input):
    parentId: UUID | None = None
    name: str = Field(min_length=1,max_length=255)

class PolicyInput(Input):
    quotaBytes: int = Field(gt=0,le=9223372036854775807,strict=True)
    retentionDays: int = Field(ge=1,le=365,strict=True)

class PermissionInput(Input):
    role: Literal['VIEWER','EDITOR']

class HoldInput(Input):
    legalHold: bool = Field(strict=True)

class ShareInput(Input):
    recipientUserId: UUID
    expiresInHours: int = Field(ge=1,le=168,strict=True)

class ResolveInput(Input):
    token: str = Field(min_length=20,max_length=512)


def require_admin(actor):
    if not actor.security_admin:
        raise Problem(403,'ACCESS_DENIED','A security administrator is required.')


def root_folder(conn, workspace_id):
    return conn.execute('SELECT id FROM folders WHERE workspace_id=%s AND parent_id IS NULL',(workspace_id,)).fetchone()['id']


def folder_json(row):
    return dict(id=row['id'],workspaceId=row['workspace_id'],parentId=row['parent_id'],name=row['name'])


def file_json(conn,row,actor=None):
    latest = conn.execute("SELECT max(version_no) AS n FROM file_versions WHERE file_id=%s AND scan_status='CLEAN'",(row['id'],)).fetchone()['n']
    role = row.get('my_role')
    if actor and not role:
        inherited = workspace_role(conn,actor.id,row['workspace_id'])
        grant = conn.execute('SELECT role FROM file_permissions WHERE file_id=%s AND user_id=%s',(row['id'],actor.id)).fetchone()
        order = {None:0,'VIEWER':1,'EDITOR':2,'OWNER':3}
        role = max([inherited,grant['role'] if grant else None],key=order.get)
    return dict(id=row['id'],workspaceId=row['workspace_id'],folderId=row['folder_id'],name=row['name'],
                latestVersionNo=row['latest_version_no'],downloadableVersionNo=latest,deletedAt=row['deleted_at'],
                purgeAt=row['purge_at'],legalHold=row['legal_hold'],updatedAt=row['updated_at'],myRole=role)


def workspace_json(conn,row,role=None):
    return dict(id=row['id'],name=row['name'],myRole=role or row.get('my_role'),quotaBytes=row['quota_bytes'],
                usedBytes=row['used_bytes'],reservedBytes=row['reserved_bytes'],rootFolderId=root_folder(conn,row['id']))


def cursor_encode(values,scope):
    data = json.dumps({'v':values,'s':scope},separators=(',',':'),default=str).encode()
    signature = hmac.new(settings.app_secret.encode(),data,hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature+data).decode().rstrip('=')


def cursor_decode(cursor,scope):
    if not cursor:
        return None
    try:
        data = base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4))
        if not hmac.compare_digest(data[:32],hmac.new(settings.app_secret.encode(),data[32:],hashlib.sha256).digest()):
            raise ValueError()
        decoded = json.loads(data[32:])
        if decoded['s']!=scope or len(decoded['v'])!=2:
            raise ValueError()
        UUID(decoded['v'][1])
        return decoded['v']
    except (ValueError,KeyError,TypeError):
        raise Problem(400,'VALIDATION_FAILED','Invalid pagination cursor.')


def ensure_recipient(conn,user_id):
    row = conn.execute("SELECT id FROM users WHERE id=%s AND status='ACTIVE'",(user_id,)).fetchone()
    if not row:
        raise Problem(404,'NOT_FOUND','Active corporate recipient not found.')


@router.get('/me')
def me(actor: Actor=Depends(get_actor)):
    return dict(id=actor.id,email=actor.email,displayName=actor.display_name,status='ACTIVE',csrfToken=actor.csrf_token,isSecurityAdmin=actor.security_admin)


@router.post('/logout',status_code=204)
def logout(request: Request,actor: Actor=Depends(get_actor)):
    with connection() as conn:
        conn.execute('UPDATE sessions SET revoked_at=now() WHERE id_hash=%s',(digest(request.cookies[settings.cookie_name]),))
        audit(conn,actor.id,'SESSION_LOGOUT',None,request.state.request_id)
    response=Response(status_code=204)
    response.delete_cookie(settings.cookie_name,path='/',secure=settings.cookie_secure,httponly=True,samesite='lax')
    return response


@router.get('/users')
def users(actor: Actor=Depends(get_actor)):
    with connection() as conn:
        return [dict(id=r['id'],email=r['email'],displayName=r['display_name']) for r in conn.execute("SELECT id,email,display_name FROM users WHERE status='ACTIVE' ORDER BY display_name").fetchall()]


@router.get('/workspaces')
def workspaces(actor: Actor=Depends(get_actor)):
    with connection() as conn:
        result=[]
        for row in conn.execute('SELECT * FROM workspaces ORDER BY name').fetchall():
            role = workspace_role(conn,actor.id,row['id'])
            if role:
                result.append(workspace_json(conn,row,role))
        return result


@router.get('/admin/workspaces')
def admin_workspaces(actor: Actor=Depends(get_actor)):
    require_admin(actor)
    with connection() as conn:
        return [workspace_json(conn,row) for row in conn.execute('SELECT * FROM workspaces ORDER BY name').fetchall()]


@router.get('/workspaces/{workspaceId}/items')
def items(workspaceId: UUID,parentId: UUID|None=None,cursor: str|None=Query(None,max_length=4096),actor: Actor=Depends(get_actor)):
    with connection() as conn:
        workspace_access(conn,actor,workspaceId)
        parent = parentId or root_folder(conn,workspaceId)
        if not conn.execute('SELECT 1 FROM folders WHERE id=%s AND workspace_id=%s',(parent,workspaceId)).fetchone():
            raise Problem(404,'NOT_FOUND','Folder not found in this workspace.')
        scope=f'items:{workspaceId}:{parent}'
        pos=cursor_decode(cursor,scope)
        rows=conn.execute('''SELECT * FROM (
            SELECT id,'FOLDER' AS kind,name,normalized_name,updated_at,NULL::text AS file_status FROM folders WHERE parent_id=%s AND workspace_id=%s
            UNION ALL
            SELECT f.id,'FILE',f.name,f.normalized_name,f.updated_at,v.scan_status FROM files f
            LEFT JOIN file_versions v ON v.file_id=f.id AND v.version_no=f.latest_version_no
            WHERE f.folder_id=%s AND f.workspace_id=%s AND f.purge_state='ACTIVE') all_items
            WHERE (%s::text IS NULL OR (normalized_name,id)>(%s,%s::uuid)) ORDER BY normalized_name,id LIMIT 101''',
            (parent,workspaceId,parent,workspaceId,pos[0] if pos else None,pos[0] if pos else None,pos[1] if pos else None)).fetchall()
        result=[dict(id=r['id'],kind=r['kind'],name=r['name'],updatedAt=r['updated_at'],fileStatus=r['file_status']) for r in rows[:100]]
        next_cursor=cursor_encode([rows[99]['normalized_name'],rows[99]['id']],scope) if len(rows)>100 else None
        return dict(items=result,nextCursor=next_cursor)


@router.post('/workspaces/{workspaceId}/folders',status_code=201)
def create_folder(workspaceId: UUID,body: FolderInput,request: Request,actor: Actor=Depends(get_actor),idempotency_key: str|None=Header(None)):
    normalized=normalize_name(body.name)
    operation=f'POST /workspaces/{workspaceId}/folders'
    with connection() as conn:
        workspace_access(conn,actor,workspaceId,'EDITOR','update')
        previous=idempotency_get(conn,actor.id,operation,idempotency_key,body.model_dump())
        if previous is not None:
            return previous
        parent=body.parentId or root_folder(conn,workspaceId)
        if not conn.execute('SELECT 1 FROM folders WHERE id=%s AND workspace_id=%s',(parent,workspaceId)).fetchone():
            raise Problem(404,'NOT_FOUND','Folder not found in this workspace.')
        if conn.execute("SELECT 1 FROM files WHERE folder_id=%s AND normalized_name=%s AND purge_state='ACTIVE'",(parent,normalized)).fetchone() or conn.execute('SELECT 1 FROM folders WHERE parent_id=%s AND normalized_name=%s',(parent,normalized)).fetchone():
            raise Problem(409,'NAME_CONFLICT','A file or folder with that name already exists.')
        row=conn.execute('INSERT INTO folders(id,workspace_id,parent_id,name,normalized_name) VALUES (%s,%s,%s,%s,%s) RETURNING *',(uuid4(),workspaceId,parent,body.name,normalized)).fetchone()
        result=folder_json(row)
        audit(conn,actor.id,'FOLDER_CREATED',row['id'],request.state.request_id,workspace_id=workspaceId)
        idempotency_put(conn,actor.id,operation,idempotency_key,body.model_dump(),result,201)
        return result


@router.get('/workspaces/{workspaceId}/policy')
def get_policy(workspaceId: UUID,actor: Actor=Depends(get_actor)):
    with connection() as conn:
        row=lock_workspace(conn,workspaceId) if actor.security_admin else workspace_access(conn,actor,workspaceId,'OWNER')
        return dict(quotaBytes=row['quota_bytes'],retentionDays=row['retention_days'])


@router.put('/workspaces/{workspaceId}/policy')
def set_policy(workspaceId: UUID,body: PolicyInput,request: Request,actor: Actor=Depends(get_actor)):
    require_admin(actor)
    with connection() as conn:
        row=lock_workspace(conn,workspaceId,'update')
        if body.quotaBytes<row['used_bytes']+row['reserved_bytes']:
            raise Problem(409,'QUOTA_EXCEEDED','Quota cannot be lower than used and reserved bytes.')
        conn.execute('UPDATE workspaces SET quota_bytes=%s,retention_days=%s WHERE id=%s',(body.quotaBytes,body.retentionDays,workspaceId))
        audit(conn,actor.id,'POLICY_UPDATED',workspaceId,request.state.request_id,workspace_id=workspaceId)
        return body.model_dump()


@router.get('/files/{fileId}')
def file_detail(fileId: UUID,actor: Actor=Depends(get_actor)):
    with connection() as conn:
        return file_json(conn,file_access(conn,actor,fileId,allow_deleted=True))


@router.get('/files/{fileId}/versions')
def versions(fileId: UUID,actor: Actor=Depends(get_actor)):
    with connection() as conn:
        file_access(conn,actor,fileId,allow_deleted=True)
        rows=conn.execute('''SELECT v.*,o.size_bytes,o.sha256 FROM file_versions v JOIN storage_objects o ON o.id=v.object_id
                             WHERE v.file_id=%s ORDER BY v.version_no DESC''',(fileId,)).fetchall()
        return [dict(versionNo=r['version_no'],sizeBytes=r['size_bytes'],sha256=r['sha256'],status=r['scan_status'],createdAt=r['created_at'],createdBy=r['created_by']) for r in rows]


@router.delete('/files/{fileId}',status_code=204)
def trash_file(fileId: UUID,request: Request,actor: Actor=Depends(get_actor)):
    with connection() as conn:
        row=file_access(conn,actor,fileId,'EDITOR','update',allow_deleted=True)
        if row['purge_state']=='TRASHED':
            return Response(status_code=204)
        policy=conn.execute('SELECT retention_days FROM workspaces WHERE id=%s',(row['workspace_id'],)).fetchone()
        conn.execute("UPDATE files SET purge_state='TRASHED',deleted_at=now(),purge_at=now()+(%s*interval '1 day'),updated_at=now() WHERE id=%s",(policy['retention_days'],fileId))
        conn.execute('UPDATE share_links SET revoked_at=COALESCE(revoked_at,now()) WHERE file_id=%s',(fileId,))
        conn.execute('UPDATE workspaces SET acl_revision=acl_revision+1 WHERE id=%s',(row['workspace_id'],))
        audit(conn,actor.id,'FILE_TRASHED',fileId,request.state.request_id,workspace_id=row['workspace_id'])
    return Response(status_code=204)


@router.get('/workspaces/{workspaceId}/trash')
def trash(workspaceId: UUID,actor: Actor=Depends(get_actor)):
    with connection() as conn:
        workspace_access(conn,actor,workspaceId,'EDITOR')
        return [file_json(conn,r,actor) for r in conn.execute("SELECT * FROM files WHERE workspace_id=%s AND purge_state='TRASHED' ORDER BY deleted_at DESC",(workspaceId,)).fetchall()]


@router.post('/files/{fileId}/recover')
def recover(fileId: UUID,request: Request,actor: Actor=Depends(get_actor),idempotency_key: str|None=Header(None)):
    operation=f'POST /files/{fileId}/recover'
    with connection() as conn:
        row=file_access(conn,actor,fileId,'EDITOR','update',allow_deleted=True)
        previous=idempotency_get(conn,actor.id,operation,idempotency_key,{})
        if previous is not None:
            return previous
        if row['purge_state']!='TRASHED':
            raise Problem(409,'INVALID_STATE','This file is not in trash.')
        if row['purge_at']<=now():
            raise Problem(409,'RETENTION_EXPIRED','The recovery retention period has expired.')
        if conn.execute("SELECT 1 FROM files WHERE folder_id=%s AND normalized_name=%s AND purge_state='ACTIVE'",(row['folder_id'],row['normalized_name'])).fetchone() or conn.execute('SELECT 1 FROM folders WHERE parent_id=%s AND normalized_name=%s',(row['folder_id'],row['normalized_name'])).fetchone():
            raise Problem(409,'NAME_CONFLICT','Another file or folder now uses this name.')
        row=conn.execute("UPDATE files SET purge_state='ACTIVE',deleted_at=NULL,purge_at=NULL,updated_at=now() WHERE id=%s RETURNING *",(fileId,)).fetchone()
        result=file_json(conn,row,actor)
        audit(conn,actor.id,'FILE_RECOVERED',fileId,request.state.request_id,workspace_id=row['workspace_id'])
        idempotency_put(conn,actor.id,operation,idempotency_key,{},result)
        return result


@router.patch('/files/{fileId}/legal-hold')
def legal_hold(fileId: UUID,body: HoldInput,request: Request,actor: Actor=Depends(get_actor)):
    require_admin(actor)
    with connection() as conn:
        loc=conn.execute('SELECT workspace_id FROM files WHERE id=%s',(fileId,)).fetchone()
        if not loc:
            raise Problem(404,'NOT_FOUND','File not found.')
        lock_workspace(conn,loc['workspace_id'],'update')
        row=conn.execute('SELECT * FROM files WHERE id=%s FOR UPDATE',(fileId,)).fetchone()
        if row['purge_state'] in ('PURGING','PURGED'):
            raise Problem(409,'FILE_PURGING','Permanent deletion has already begun.')
        conn.execute('UPDATE files SET legal_hold=%s,updated_at=now() WHERE id=%s',(body.legalHold,fileId))
        audit(conn,actor.id,'LEGAL_HOLD_SET' if body.legalHold else 'LEGAL_HOLD_REMOVED',fileId,request.state.request_id,workspace_id=row['workspace_id'])
        return dict(fileId=fileId,legalHold=body.legalHold)


@router.get('/files/{fileId}/permissions')
def permissions(fileId: UUID,actor: Actor=Depends(get_actor)):
    with connection() as conn:
        file_access(conn,actor,fileId,'OWNER',allow_deleted=True)
        return [dict(userId=r['user_id'],role=r['role']) for r in conn.execute('SELECT user_id,role FROM file_permissions WHERE file_id=%s ORDER BY user_id',(fileId,)).fetchall()]


@router.put('/files/{fileId}/permissions/{userId}')
def grant(fileId: UUID,userId: UUID,body: PermissionInput,request: Request,actor: Actor=Depends(get_actor)):
    with connection() as conn:
        row=file_access(conn,actor,fileId,'OWNER','update')
        ensure_recipient(conn,userId)
        conn.execute('''INSERT INTO file_permissions(file_id,user_id,role) VALUES (%s,%s,%s)
                        ON CONFLICT(file_id,user_id) DO UPDATE SET role=excluded.role''',(fileId,userId,body.role))
        conn.execute('UPDATE workspaces SET acl_revision=acl_revision+1 WHERE id=%s',(row['workspace_id'],))
        audit(conn,actor.id,'PERMISSION_GRANTED',fileId,request.state.request_id,workspace_id=row['workspace_id'])
        return dict(fileId=fileId,userId=userId,role=body.role)


@router.delete('/files/{fileId}/permissions/{userId}',status_code=204)
def revoke_permission(fileId: UUID,userId: UUID,request: Request,actor: Actor=Depends(get_actor)):
    with connection() as conn:
        row=file_access(conn,actor,fileId,'OWNER','update',allow_deleted=True)
        conn.execute('DELETE FROM file_permissions WHERE file_id=%s AND user_id=%s',(fileId,userId))
        conn.execute('UPDATE workspaces SET acl_revision=acl_revision+1 WHERE id=%s',(row['workspace_id'],))
        audit(conn,actor.id,'PERMISSION_REVOKED',fileId,request.state.request_id,workspace_id=row['workspace_id'])
    return Response(status_code=204)


@router.get('/files/{fileId}/share-links')
def share_links(fileId: UUID,actor: Actor=Depends(get_actor)):
    with connection() as conn:
        file_access(conn,actor,fileId,'OWNER',allow_deleted=True)
        rows=conn.execute('SELECT * FROM share_links WHERE file_id=%s ORDER BY created_at DESC',(fileId,)).fetchall()
        return [dict(id=r['id'],recipientUserId=r['recipient_user_id'],expiresAt=r['expires_at'],revokedAt=r['revoked_at']) for r in rows]


@router.post('/files/{fileId}/share-links',status_code=201)
def share(fileId: UUID,body: ShareInput,request: Request,actor: Actor=Depends(get_actor),idempotency_key: str|None=Header(None)):
    operation=f'POST /files/{fileId}/share-links'
    with connection() as conn:
        row=file_access(conn,actor,fileId,'OWNER','update')
        ensure_recipient(conn,body.recipientUserId)
        previous=idempotency_get(conn,actor.id,operation,idempotency_key,body.model_dump())
        if previous is not None:
            valid=conn.execute('SELECT 1 FROM share_links WHERE id=%s AND revoked_at IS NULL AND expires_at>now()',(previous['id'],)).fetchone()
            if not valid:
                raise Problem(409,'SHARE_REVOKED','This previously created link has expired or been revoked.')
            return previous
        if not conn.execute("SELECT 1 FROM file_versions WHERE file_id=%s AND scan_status='CLEAN'",(fileId,)).fetchone():
            raise Problem(422,'QUARANTINED','Sharing requires at least one clean file version.')
        secret=secrets.token_urlsafe(32)
        share_id=uuid4()
        expiry=now()+timedelta(hours=body.expiresInHours)
        conn.execute('INSERT INTO share_links(id,file_id,recipient_user_id,created_by,secret_hash,expires_at) VALUES (%s,%s,%s,%s,%s,%s)',
                     (share_id,fileId,body.recipientUserId,actor.id,digest(secret),expiry))
        result=dict(id=share_id,url=settings.app_origin+'/#share='+secret,recipientUserId=body.recipientUserId,expiresAt=expiry)
        conn.execute('UPDATE workspaces SET acl_revision=acl_revision+1 WHERE id=%s',(row['workspace_id'],))
        audit(conn,actor.id,'SHARE_CREATED',fileId,request.state.request_id,workspace_id=row['workspace_id'])
        idempotency_put(conn,actor.id,operation,idempotency_key,body.model_dump(),result,201,sensitive=True)
        return result


@router.delete('/share-links/{shareLinkId}',status_code=204)
def revoke_share(shareLinkId: UUID,request: Request,actor: Actor=Depends(get_actor)):
    with connection() as conn:
        share=conn.execute('SELECT file_id FROM share_links WHERE id=%s',(shareLinkId,)).fetchone()
        if not share:
            raise Problem(404,'NOT_FOUND','Share link not found.')
        row=file_access(conn,actor,share['file_id'],'OWNER','update',allow_deleted=True)
        conn.execute('UPDATE share_links SET revoked_at=COALESCE(revoked_at,now()) WHERE id=%s',(shareLinkId,))
        conn.execute('UPDATE workspaces SET acl_revision=acl_revision+1 WHERE id=%s',(row['workspace_id'],))
        audit(conn,actor.id,'SHARE_REVOKED',share['file_id'],request.state.request_id,workspace_id=row['workspace_id'])
    return Response(status_code=204)


@router.post('/share-links/resolve')
def resolve_share(body: ResolveInput,request: Request,actor: Actor=Depends(get_actor)):
    from .uploads import download_for_file
    with connection() as conn:
        loc=conn.execute('''SELECT f.workspace_id FROM share_links s JOIN files f ON f.id=s.file_id
                            WHERE s.secret_hash=%s''',(digest(body.token),)).fetchone()
        if not loc:
            raise Problem(404,'NOT_FOUND','Share link not found.')
        lock_workspace(conn,loc['workspace_id'])
        share=conn.execute('SELECT * FROM share_links WHERE secret_hash=%s FOR SHARE',(digest(body.token),)).fetchone()
        if share['recipient_user_id']!=actor.id:
            raise Problem(403,'ACCESS_DENIED','This link is assigned to another corporate recipient.')
        if share['revoked_at'] or share['expires_at']<=now():
            raise Problem(404,'NOT_FOUND','Share link has expired or been revoked.')
        row=conn.execute("SELECT * FROM files WHERE id=%s AND purge_state='ACTIVE' FOR SHARE",(share['file_id'],)).fetchone()
        if not row:
            raise Problem(404,'NOT_FOUND','Shared file is no longer available.')
        download=download_for_file(conn,row,actor,request.state.request_id)
        audit(conn,actor.id,'SHARE_RESOLVED',row['id'],request.state.request_id,workspace_id=row['workspace_id'])
        return dict(fileId=row['id'],fileName=row['name'],download=download)


@router.get('/search')
def search(workspaceId: UUID,q: str=Query(min_length=2,max_length=128),cursor: str|None=Query(None,max_length=4096),actor: Actor=Depends(get_actor)):
    with connection() as conn:
        lock_workspace(conn,workspaceId)
        inherited=workspace_role(conn,actor.id,workspaceId)
        if not inherited and not conn.execute('''SELECT 1 FROM files f WHERE f.workspace_id=%s AND
            EXISTS(SELECT 1 FROM file_permissions p WHERE p.file_id=f.id AND p.user_id=%s) LIMIT 1''',
            (workspaceId,actor.id)).fetchone():
            raise Problem(403,'ACCESS_DENIED','You do not have access to files in this workspace.')
        scope=f'search:{workspaceId}:{q}:{actor.id}'
        pos=cursor_decode(cursor,scope)
        # Escape SQL wildcards: this API searches literal filename fragments.
        pattern='%'+q.casefold().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        rows=conn.execute('''SELECT * FROM files WHERE workspace_id=%s AND purge_state='ACTIVE' AND normalized_name LIKE %s
             AND (%s OR EXISTS(SELECT 1 FROM file_permissions p WHERE p.file_id=files.id AND p.user_id=%s))
             AND (%s::text IS NULL OR (normalized_name,id)>(%s,%s::uuid)) ORDER BY normalized_name,id LIMIT 101''',
             (workspaceId,pattern,bool(inherited),actor.id,pos[0] if pos else None,pos[0] if pos else None,pos[1] if pos else None)).fetchall()
        return dict(items=[file_json(conn,row,actor) for row in rows[:100]],
                    nextCursor=cursor_encode([rows[99]['normalized_name'],rows[99]['id']],scope) if len(rows)>100 else None)


@router.get('/audit-events')
def audit_events(workspaceId: UUID|None=None,cursor: str|None=Query(None,max_length=4096),actor: Actor=Depends(get_actor)):
    require_admin(actor)
    scope=f'audit:{workspaceId}'
    pos=cursor_decode(cursor,scope)
    if pos:
        try:
            datetime.fromisoformat(pos[0])
        except ValueError:
            raise Problem(400,'VALIDATION_FAILED','Invalid pagination cursor.')
    with connection() as conn:
        rows=conn.execute('''SELECT * FROM audit_events WHERE (%s::uuid IS NULL OR workspace_id=%s)
         AND (%s::timestamptz IS NULL OR (created_at,id)<(%s::timestamptz,%s::uuid)) ORDER BY created_at DESC,id DESC LIMIT 101''',
         (workspaceId,workspaceId,pos[0] if pos else None,pos[0] if pos else None,pos[1] if pos else None)).fetchall()
        return dict(items=[dict(id=r['id'],at=r['created_at'],actorUserId=r['actor_id'],action=r['action'],targetId=r['target_id'],result=r['result'],requestId=r['request_id']) for r in rows[:100]],
                    nextCursor=cursor_encode([rows[99]['created_at'],rows[99]['id']],scope) if len(rows)>100 else None)


@router.get('/admin/jobs')
def admin_jobs(actor: Actor=Depends(get_actor)):
    require_admin(actor)
    with connection() as conn:
        return conn.execute("SELECT id,kind,target_id,state,attempts,error_code,updated_at FROM jobs WHERE state='FAILED' ORDER BY updated_at DESC LIMIT 100").fetchall()


@router.post('/admin/jobs/{jobId}/retry')
def retry_job(jobId: UUID,request: Request,actor: Actor=Depends(get_actor)):
    require_admin(actor)
    with connection() as conn:
        row=conn.execute("UPDATE jobs SET state='QUEUED',attempts=0,lease_token=NULL,lease_until=NULL,next_run_at=now(),updated_at=now() WHERE id=%s AND state='FAILED' RETURNING id",(jobId,)).fetchone()
        if not row:
            raise Problem(409,'INVALID_STATE','Only a failed job can be retried.')
        audit(conn,actor.id,'JOB_RETRIED',jobId,request.state.request_id)
        return dict(id=jobId,status='QUEUED')
