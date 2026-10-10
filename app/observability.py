"""Bounded-cardinality service metrics; no request or resource IDs become labels."""
import json
import threading
import time
from collections import defaultdict
from contextlib import contextmanager

from .db import connection

HTTP_BUCKETS=(.05,.1,.25,.5,.7,1,2,5,float('inf'))
SCAN_BUCKETS=(1,5,15,30,60,120,300,600,float('inf'))
_lock=threading.Lock()
_http=defaultdict(int)
_durations={}
_dependencies=defaultdict(int)
_dropped=0


def size_bucket(size):
    return 'le_1mib' if size<=1024**2 else '1_10mib' if size<=10*1024**2 else '10_100mib' if size<=100*1024**2 else '100mib_2gib'


def observe_http(route,method,status,seconds):
    method=method if method in {'GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS'} else 'OTHER'
    group='browse_search' if route.endswith('/items') or route.endswith('/search') else 'other'
    with _lock:
        _http[(route,method,f'{status//100}xx')]+=1
        row=_durations.setdefault((route,method,group),[0,0.0,[0]*len(HTTP_BUCKETS)])
        row[0]+=1
        row[1]+=seconds
        for i,bound in enumerate(HTTP_BUCKETS):
            if seconds<=bound:
                row[2][i]+=1


def dependency_result(dependency,operation,result):
    if dependency not in {'oidc','storage','av'}:
        raise ValueError('Unknown dependency')
    with _lock:
        _dependencies[(dependency,operation,result)]+=1


@contextmanager
def dependency(dependency_name,operation):
    try:
        yield
    except Exception:
        dependency_result(dependency_name,operation,'failure')
        raise
    else:
        dependency_result(dependency_name,operation,'success')


def analytics_dropped():
    global _dropped
    with _lock:
        _dropped+=1


def dropped_count():
    with _lock:
        return _dropped


def observe_scan(seconds,outcome,size_bytes):
    if outcome not in {'clean','rejected','technical_failure'}:
        raise ValueError('Unknown scan outcome')
    # Worker counters survive worker/API restarts and are available in one scrape.
    from psycopg.types.json import Jsonb
    bounds=[str(b) if b!=float('inf') else '+Inf' for b in SCAN_BUCKETS]
    counts={b:int(seconds<=n) for b,n in zip(bounds,SCAN_BUCKETS)}
    with connection() as conn:
        conn.execute('''INSERT INTO observability_scans(outcome,size_bucket,count,total,buckets) VALUES (%s,%s,1,%s,%s)
            ON CONFLICT(outcome,size_bucket) DO UPDATE SET count=observability_scans.count+1,total=observability_scans.total+excluded.total,
            buckets=(SELECT jsonb_object_agg(key,COALESCE((observability_scans.buckets->>key)::bigint,0)+value::bigint)
                     FROM jsonb_each_text(excluded.buckets))''',(outcome,size_bucket(size_bytes),seconds,Jsonb(counts)))


def labels(**values):
    return '{'+','.join(k+'='+json.dumps(str(v)) for k,v in values.items())+'}' if values else ''


def render_metrics():
    with _lock:
        reqs=dict(_http)
        durations={k:[v[0],v[1],list(v[2])] for k,v in _durations.items()}
        dependencies=dict(_dependencies)
        dropped=_dropped
    output=['# TYPE fileflow_http_requests_total counter']
    for (route,method,status),count in reqs.items():
        output.append(f'fileflow_http_requests_total{labels(route=route,method=method,status_class=status)} {count}')
    output.append('# TYPE fileflow_http_request_duration_seconds histogram')
    for (route,method,group),(count,total,buckets) in durations.items():
        common=dict(route=route,method=method,route_group=group)
        for bound,value in zip(HTTP_BUCKETS,buckets):
            output.append(f'fileflow_http_request_duration_seconds_bucket{labels(**common,le="+Inf" if bound==float("inf") else bound)} {value}')
        output.extend([f'fileflow_http_request_duration_seconds_count{labels(**common)} {count}',f'fileflow_http_request_duration_seconds_sum{labels(**common)} {total}'])
    output.append('# TYPE fileflow_dependency_requests_total counter')
    for (dep,operation,result),count in dependencies.items():
        output.append(f'fileflow_dependency_requests_total{labels(dependency=dep,operation=operation,result=result)} {count}')
    output.extend(['# TYPE analytics_events_dropped_total counter',f'analytics_events_dropped_total {dropped}'])
    with connection() as conn:
        jobs=conn.execute('''SELECT kind,state,count(*) AS n FROM jobs GROUP BY kind,state''').fetchall()
        ages=conn.execute("SELECT kind,EXTRACT(EPOCH FROM now()-min(created_at)) AS age FROM jobs WHERE state IN ('QUEUED','RUNNING') GROUP BY kind").fetchall()
        failed=conn.execute('SELECT kind,reason,count FROM observability_job_failures').fetchall()
        scans=conn.execute('SELECT * FROM observability_scans').fetchall()
        db=conn.execute("""SELECT count(*) FILTER(WHERE backend_type='client backend') AS connections,
             COALESCE(max(EXTRACT(EPOCH FROM now()-query_start)) FILTER(WHERE wait_event_type='Lock'),0) AS lock_wait
             FROM pg_stat_activity WHERE datname=current_database()""").fetchone()
        budget=conn.execute("SELECT current_setting('max_connections')::integer-current_setting('superuser_reserved_connections')::integer AS n").fetchone()['n']
        pending=conn.execute("SELECT count(*) AS n FROM file_versions WHERE scan_status='PENDING_SCAN'").fetchone()['n']
    output.append('# TYPE fileflow_jobs gauge')
    lookup={(j['kind'],j['state']):j['n'] for j in jobs}
    for kind in ('SCAN','PURGE','CLEANUP','COMPLETE'):
        for state in ('QUEUED','RUNNING','DONE','FAILED'):
            output.append(f'fileflow_jobs{labels(job_kind=kind,state=state)} {lookup.get((kind,state),0)}')
    output.append('# TYPE fileflow_jobs_oldest_unfinished_age_seconds gauge')
    by_kind={a['kind']:a['age'] for a in ages}
    for kind in ('SCAN','PURGE','CLEANUP','COMPLETE'):
        output.append(f'fileflow_jobs_oldest_unfinished_age_seconds{labels(job_kind=kind)} {by_kind.get(kind,0)}')
    output.append('# TYPE fileflow_jobs_failed_total counter')
    for row in failed:
        output.append(f'fileflow_jobs_failed_total{labels(job_kind=row["kind"],reason_code=row["reason"])} {row["count"]}')
    output.append('# TYPE fileflow_scan_duration_seconds histogram')
    for row in scans:
        common=dict(outcome=row['outcome'],size_bucket=row['size_bucket'])
        for bound in SCAN_BUCKETS:
            key='+Inf' if bound==float('inf') else str(bound)
            output.append(f'fileflow_scan_duration_seconds_bucket{labels(**common,le=key)} {row["buckets"].get(key,0)}')
        output.extend([f'fileflow_scan_duration_seconds_count{labels(**common)} {row["count"]}',f'fileflow_scan_duration_seconds_sum{labels(**common)} {row["total"]}'])
    output.extend(['# TYPE fileflow_db_connections gauge',f'fileflow_db_connections {db["connections"]}',
                   '# TYPE fileflow_db_connection_budget gauge',f'fileflow_db_connection_budget {budget}',
                   '# TYPE fileflow_db_lock_wait_seconds gauge',f'fileflow_db_lock_wait_seconds {db["lock_wait"]}',
                   '# TYPE fileflow_pending_scans gauge',f'fileflow_pending_scans {pending}'])
    return '\n'.join(output)+'\n'
