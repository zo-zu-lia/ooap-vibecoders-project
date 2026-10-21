"""Privacy-conscious product measurements from authoritative facts and server events.

This is an on-demand projection over a small company's PostgreSQL data. Read
observations are bounded and asynchronous; mutation facts stay in domain/audit
transactions. A failed analytics write never blocks browse or search.
"""
import hashlib
import hmac
import json
import logging
import queue
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import UUID, NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query
from psycopg.types.json import Jsonb

from .config import settings
from .db import connection
from .errors import Problem
from .observability import analytics_dropped,dropped_count
from .security import Actor,get_actor,workspace_access

router=APIRouter(prefix='/api/v1')
log=logging.getLogger('fileflow.analytics')
EVENTS={'activity.authorized','download.link_issued','share.resolve_attempted','share.resolve_succeeded',
        'search.executed','version.complete_attempted','file.recover_attempted'}
REASONS={'VERSION_CONFLICT','FILE_PURGING','RETENTION_EXPIRED','ACCESS_DENIED','NOT_FOUND','DEPENDENCY_UNAVAILABLE','BAD_REQUEST','QUARANTINED','FILE_TRASHED','INVALID_STATE'}
_queue=queue.Queue(maxsize=2048)
_stop=threading.Event()
_thread=None


def record_event(conn,event_name,actor_id=None,workspace_id=None,resource_id=None,version_no=None,
                 operation_id=None,result='SUCCESS',reason_code=None,result_bucket=None,occurred_at=None):
    if event_name not in EVENTS:
        raise ValueError('Unsupported analytics event')
    if result_bucket not in (None,'zero','one_or_more'):
        raise ValueError('Unsupported result bucket')
    actor_key=hmac.new(settings.app_secret.encode(),('analytics:'+str(actor_id)).encode(),hashlib.sha256).hexdigest() if actor_id else None
    operation_key=hashlib.sha256(f'{event_name}:{actor_key}:{resource_id}:{operation_id}'.encode()).hexdigest()
    try:
        with conn.transaction():
            conn.execute('''INSERT INTO analytics_events(id,event_name,actor_key,workspace_id,resource_id,version_no,operation_key,result,reason_code,result_bucket,occurred_at,is_synthetic)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(operation_key) DO NOTHING''',
               (uuid5(NAMESPACE_URL,operation_key),event_name,actor_key,workspace_id,resource_id,version_no,operation_key,
                'SUCCESS' if result=='SUCCESS' else 'FAILED',reason_code if reason_code in REASONS else ('OTHER' if reason_code else None),
                result_bucket,occurred_at or datetime.now(timezone.utc),settings.seed_demo))
    except Exception:
        analytics_dropped()
        log.warning('Analytics observation dropped')


def enqueue_event(**event):
    try:
        _queue.put_nowait(event)
    except queue.Full:
        analytics_dropped()


def _consume():
    next_cleanup=time.monotonic()+300
    while not _stop.is_set() or not _queue.empty():
        try:
            event=_queue.get(timeout=.5)
        except queue.Empty:
            event=None
        if event:
            try:
                with connection() as conn:
                    # Resource-to-workspace lookup is server-side; never record URL/query text.
                    if not event.get('workspace_id') and event.get('resource_id'):
                        row=conn.execute('SELECT workspace_id FROM files WHERE id=%s UNION ALL SELECT workspace_id FROM upload_sessions WHERE id=%s LIMIT 1',
                                         (event['resource_id'],event['resource_id'])).fetchone()
                        if row:
                            event['workspace_id']=row['workspace_id']
                    record_event(conn,**event)
            except Exception:
                analytics_dropped()
                log.warning('Analytics observation dropped')
            finally:
                _queue.task_done()
        if time.monotonic()>=next_cleanup:
            try:
                project_daily()
            except Exception:
                log.warning('Analytics daily projection unavailable')
            next_cleanup=time.monotonic()+300


def start():
    global _thread
    _stop.clear()
    _thread=threading.Thread(target=_consume,name='analytics',daemon=True)
    _thread.start()


def stop():
    _stop.set()
    if _thread:
        _thread.join(timeout=5)


def observe_request(request,status):
    actor_id=getattr(request.state,'actor_id',None)
    if not actor_id:
        return
    params=request.path_params
    route=getattr(request.scope.get('route'),'path','')
    workspace_id=params.get('workspaceId') or getattr(request.state,'analytics_workspace',None)
    resource_id=params.get('fileId') or params.get('file_id') or params.get('upload_id')
    attempt=getattr(request.state,'analytics_attempt',None)
    if attempt and status<500 and status!=429:
        enqueue_event(actor_id=actor_id,**attempt,result='SUCCESS' if status<400 else 'FAILED',reason_code=getattr(request.state,'problem_code',None))
    if 200<=status<300:
        # Polling, session reads, admin screens, and metrics never inflate activity.
        active=(route.endswith('/items') or route.endswith('/search') or route.endswith('/download-link') or
                route.endswith('/uploads') or route.endswith('/share-links') or route.endswith('/share-links/resolve'))
        if active:
            enqueue_event(event_name='activity.authorized',actor_id=actor_id,workspace_id=workspace_id,
                          resource_id=resource_id,operation_id=request.state.request_id)
        search=getattr(request.state,'analytics_search',None)
        if search:
            enqueue_event(event_name='search.executed',actor_id=actor_id,workspace_id=search['workspace_id'],
                          operation_id=request.state.request_id,result_bucket=search['result_bucket'])


def _percent(n,d):
    return round(100*n/d,2) if d else None


def _metric(id,label,value,unit='count',sample=None,status=None,**extra):
    return dict(id=id,label=label,value=value,unit=unit,sampleSize=sample,
                status=status or ('observed' if value is not None else 'unavailable'),**extra)


def summarize(conn,start,end,workspace_id=None):
    """All joins use stable domain IDs; repeated delivery/HTTP retries cannot inflate facts."""
    scope='(%s::uuid IS NULL OR workspace_id=%s)'
    args=(workspace_id,workspace_id,start,end)
    activity=conn.execute(f'''SELECT count(DISTINCT actor_key) AS n FROM analytics_events
       WHERE {scope} AND occurred_at>=%s AND occurred_at<%s AND event_name='activity.authorized' AND result='SUCCESS' ''',args).fetchone()['n']
    today=datetime.now(ZoneInfo('Europe/Kyiv')).replace(hour=0,minute=0,second=0,microsecond=0)
    daily=conn.execute(f'''SELECT count(DISTINCT actor_key) AS n FROM analytics_events WHERE {scope}
       AND occurred_at>=%s AND occurred_at<%s AND event_name='activity.authorized' AND result='SUCCESS' ''',
       (workspace_id,workspace_id,max(start,today),end)).fetchone()['n']
    uploads=conn.execute('''SELECT u.id,u.created_at,u.expected_version_no,v.created_at AS committed_at,
         a.created_at AS ready_at,
         EXISTS(SELECT 1 FROM analytics_events e WHERE e.event_name='download.link_issued' AND e.result='SUCCESS'
            AND e.resource_id=u.file_id AND e.version_no=u.version_no AND e.occurred_at>=a.created_at
            AND e.occurred_at<=u.created_at+interval '24 hours') AS linked
         FROM upload_sessions u LEFT JOIN file_versions v ON v.upload_id=u.id
         LEFT JOIN audit_events a ON a.effect_key='scan:'||u.id::text AND a.action='SCAN_CLEAN'
         WHERE (%s::uuid IS NULL OR u.workspace_id=%s) AND u.created_at>=%s AND u.created_at<%s''',args).fetchall()
    observed_at=datetime.now(timezone.utc)
    mature=[u for u in uploads if u['created_at']+timedelta(hours=24)<=observed_at]
    committed=lambda u: bool(u['committed_at'] and u['committed_at']<=u['created_at']+timedelta(hours=24))
    ready=lambda u: bool(committed(u) and u['ready_at'] and u['ready_at']<=u['created_at']+timedelta(hours=24))
    latency_rows=conn.execute('''SELECT EXTRACT(EPOCH FROM a.created_at-u.created_at) AS elapsed FROM upload_sessions u
      JOIN audit_events a ON a.effect_key='scan:'||u.id::text AND a.action='SCAN_CLEAN'
      WHERE (%s::uuid IS NULL OR u.workspace_id=%s) AND a.created_at>=%s AND a.created_at<%s
      AND a.created_at<=u.created_at+interval '24 hours' ''',args).fetchall()
    ready_times=sorted(float(r['elapsed']) for r in latency_rows)
    def quantile(values,q):
        if not values:
            return None
        point=(len(values)-1)*q
        low=int(point)
        high=min(low+1,len(values)-1)
        return round(values[low]+(values[high]-values[low])*(point-low),3)
    restores=conn.execute(f"SELECT count(*) AS n FROM audit_events WHERE {scope} AND created_at>=%s AND created_at<%s AND action='VERSION_RESTORED'",args).fetchone()['n']
    downloads=conn.execute(f"SELECT count(*) AS n FROM audit_events WHERE {scope} AND created_at>=%s AND created_at<%s AND action='DOWNLOAD_LINK_ISSUED'",args).fetchone()['n']
    shares=conn.execute('''SELECT s.id,s.expires_at,s.revoked_at,
       EXISTS(SELECT 1 FROM analytics_events e WHERE e.resource_id=s.id AND e.event_name='share.resolve_attempted'
          AND e.occurred_at>=s.created_at AND e.occurred_at<s.expires_at) AS attempted,
       EXISTS(SELECT 1 FROM analytics_events e WHERE e.resource_id=s.id AND e.event_name='share.resolve_succeeded'
          AND e.result='SUCCESS' AND e.occurred_at>=s.created_at AND e.occurred_at<s.expires_at
          AND (s.revoked_at IS NULL OR e.occurred_at<=s.revoked_at)) AS resolved
       FROM share_links s JOIN files f ON f.id=s.file_id
       WHERE (%s::uuid IS NULL OR f.workspace_id=%s) AND s.created_at>=%s AND s.created_at<%s''',args).fetchall()
    closed=[s for s in shares if s['expires_at']<=observed_at]
    searches=conn.execute(f'''SELECT count(*) AS total,count(*) FILTER(WHERE result_bucket='zero') AS empty
       FROM analytics_events WHERE {scope} AND occurred_at>=%s AND occurred_at<%s
       AND event_name='search.executed' AND result='SUCCESS' ''',args).fetchone()
    attempts=conn.execute(f'''SELECT event_name,count(*) AS total,count(*) FILTER(WHERE result='SUCCESS') AS successes,
        count(*) FILTER(WHERE reason_code='VERSION_CONFLICT') AS conflicts FROM analytics_events WHERE {scope}
        AND occurred_at>=%s AND occurred_at<%s AND event_name IN ('version.complete_attempted','file.recover_attempted') GROUP BY event_name''',args).fetchall()
    outcomes={r['event_name']:r for r in attempts}
    complete_attempt=outcomes.get('version.complete_attempted',dict(total=0,conflicts=0))
    recover_attempt=outcomes.get('file.recover_attempted',dict(total=0,successes=0))
    metrics=[
        _metric('B01','Active employees',activity,'people',activity,dailyActive=daily),
        _metric('B02','Uploads started',len(uploads),sample=len(uploads)),
        _metric('B03','Upload completion',_percent(sum(committed(u) for u in mature),len(mature)),'percent',len(mature),
                status='observed' if mature else ('provisional' if uploads else 'unavailable')),
        _metric('B04','Uploads ready within 24 hours',_percent(sum(ready(u) for u in mature),len(mature)),'percent',len(mature),
                status='observed' if mature else ('provisional' if uploads else 'unavailable')),
        _metric('B05','Time to ready (p95)',quantile(ready_times,.95),'seconds',len(ready_times),p50=quantile(ready_times,.5)),
        _metric('B06','Previous versions restored',restores,sample=restores),
        _metric('B07','Download links issued',downloads,sample=downloads),
        _metric('B08','Internal share links created',len(shares),sample=len(shares)),
        _metric('B09','Recipient sharing conversion',_percent(sum(s['resolved'] for s in closed),len(closed)),'percent',len(closed),
                status='observed' if closed else ('provisional' if shares else 'unavailable')),
        _metric('B10','Searches with no results',_percent(searches['empty'],searches['total']),'percent',searches['total']),
        _metric('B11','Version completion conflicts',_percent(complete_attempt['conflicts'],complete_attempt['total']),'percent',complete_attempt['total']),
        _metric('B12','Trash recovery success',_percent(recover_attempt['successes'],recover_attempt['total']),'percent',recover_attempt['total'])]
    funnels=dict(upload=dict(initiated=len(uploads),committed=sum(committed(u) for u in uploads),ready=sum(ready(u) for u in uploads),
                              linkIssued=sum(ready(u) and u['linked'] for u in uploads),mature=len(mature),provisional=len(uploads)-len(mature)),
                 sharing=dict(created=len(shares),attempted=sum(s['attempted'] for s in shares),resolved=sum(s['resolved'] for s in shares),
                              mature=len(closed),provisional=len(shares)-len(closed)))
    return dict(metrics=metrics,funnels=funnels,sampleActors=activity)


def project_daily():
    """Idempotent snapshots contain only aggregate counts, never actor/resource IDs."""
    zone=ZoneInfo('Europe/Kyiv')
    today=datetime.now(zone).replace(hour=0,minute=0,second=0,microsecond=0)
    with connection() as conn:
        # One instance at a time; late commits are included by recomputing seven days.
        if not conn.execute('SELECT pg_try_advisory_xact_lock(492815091) AS ok').fetchone()['ok']:
            return
        scopes=[None]+[r['id'] for r in conn.execute('SELECT id FROM workspaces').fetchall()]
        for age in range(1,8):
            day=today-timedelta(days=age)
            for scope in scopes:
                summary=summarize(conn,day,day+timedelta(days=1),scope)
                # Store aggregate values only; pseudonym distinct counts are ephemeral.
                conn.execute('''INSERT INTO analytics_daily(day,scope,metrics) VALUES (%s,%s,%s)
                   ON CONFLICT(day,scope) DO UPDATE SET metrics=excluded.metrics,updated_at=now()''',
                   (day.date(),str(scope) if scope else 'global',Jsonb(summary)))
        conn.execute("DELETE FROM analytics_events WHERE occurred_at<now()-interval '30 days'")
        conn.execute("DELETE FROM analytics_daily WHERE day<(now() AT TIME ZONE 'Europe/Kyiv')::date-180")


@router.get('/analytics')
def dashboard(workspaceId: UUID|None=None,days: int=Query(7,ge=1,le=30),actor: Actor=Depends(get_actor)):
    end=datetime.now(timezone.utc)
    start=(datetime.now(ZoneInfo('Europe/Kyiv')).replace(hour=0,minute=0,second=0,microsecond=0)-timedelta(days=days-1)).astimezone(timezone.utc)
    with connection() as conn:
        if not actor.security_admin:
            if workspaceId is None:
                raise Problem(403,'ACCESS_DENIED','Choose a workspace you own to view analytics.')
            workspace_access(conn,actor,workspaceId,'OWNER')
        elif workspaceId and not conn.execute('SELECT 1 FROM workspaces WHERE id=%s',(workspaceId,)).fetchone():
            raise Problem(404,'NOT_FOUND','Workspace not found.')
        summary=summarize(conn,start,end,workspaceId)
        started=conn.execute("SELECT value FROM app_metadata WHERE key='analytics_started_at'").fetchone()
    started_at=datetime.fromisoformat(started['value']) if started else end
    incomplete=dropped_count()>0 or started_at>start
    suppressed=not actor.security_admin and summary['sampleActors']<5
    if suppressed:
        for metric in summary['metrics']:
            metric.update(value=None,sampleSize=None,status='suppressed')
            for key in ('p50','dailyActive'):
                if key in metric:
                    metric[key]=None
        summary['funnels']={name:{key:None for key in values} for name,values in summary['funnels'].items()}
        summary['sampleActors']=None
    notes=['Download links measure authorization and URL issuance, not completed byte transfers.',
           'Upload conversion requires a full 24-hour observation window; share conversion requires expiry.',
           'Read-event collection began at '+started_at.isoformat()+'.']
    if incomplete:
        notes.append('This period contains time before instrumentation or dropped observations; read-based metrics may be incomplete.')
    if settings.seed_demo:
        notes.append('Local demo traffic is synthetic and is included for diagnostic use.')
    if suppressed:
        notes.append('Workspace statistics are hidden until at least five distinct employees are observed.')
    return dict(period=dict(start=start,end=end,timezone='Europe/Kyiv'),updatedAt=end,
                privacySuppressed=suppressed,quality=dict(isSynthetic=settings.seed_demo,incomplete=incomplete,notes=notes),**summary)
