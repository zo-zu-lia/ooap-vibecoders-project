"""Bounded PostgreSQL connections with explicit transaction boundaries."""
from contextlib import contextmanager
import os
import threading
from pathlib import Path
from uuid import UUID
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
from .config import settings

_pool=None
_pool_lock=threading.Lock()


def pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                size=max(1,min(16,int(os.getenv('DB_POOL_SIZE','8'))))
                _pool=ConnectionPool(settings.database_url,min_size=1,max_size=size,
                    timeout=8,max_waiting=80,open=False,
                    kwargs=dict(row_factory=dict_row,connect_timeout=5,
                                application_name=os.getenv('APP_COMPONENT','fileflow'),
                                options='-c statement_timeout=15000 -c lock_timeout=10000'))
                _pool.open(wait=False)
    return _pool


@contextmanager
def connection():
    with pool().connection() as conn:
        yield conn


def close_pool():
    if _pool:
        _pool.close()


def initialize():
    with connection() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(438927105)')
        conn.execute(Path(__file__).with_name('schema.sql').read_text())
        if settings.seed_demo:
            seed(conn)
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname='fileflow_app'").fetchone():
            conn.execute('GRANT USAGE ON SCHEMA public TO fileflow_app')
            conn.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA public TO fileflow_app')
            conn.execute('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA public TO fileflow_app')
            conn.execute('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO fileflow_app')
            conn.execute('REVOKE UPDATE,DELETE,TRUNCATE ON audit_events FROM fileflow_app')
            conn.execute('GRANT SELECT,INSERT ON audit_events TO fileflow_app')


def demo_id(n):
    return UUID(f'00000000-0000-4000-8000-{n:012d}')


def seed(conn):
    # Seed once. Re-running migrations must never recreate revoked grants.
    inserted = conn.execute("INSERT INTO app_metadata(key,value) VALUES ('local_demo_seed','1') ON CONFLICT DO NOTHING RETURNING key").fetchone()
    if not inserted:
        return
    for n, email, name, admin in [(1,'owner','Alex Morgan',False),(2,'editor','Sam Chen',False),
                                  (3,'viewer','Taylor Reed',False),(4,'security','Jordan Security',True),
                                  (5,'finance','Casey Finance',False)]:
        conn.execute('INSERT INTO users(id,email,display_name,security_admin) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                     (demo_id(n),email+'@fileflow.local',name,admin))
        conn.execute("INSERT INTO principals(id,kind,user_id) VALUES (%s,'USER',%s) ON CONFLICT DO NOTHING",(demo_id(n),demo_id(n)))
    for n,name in [(10,'Design team'),(20,'Finance')]:
        conn.execute('INSERT INTO workspaces(id,name,quota_bytes) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',(demo_id(n),name,5*1024**3))
        conn.execute("INSERT INTO folders(id,workspace_id,parent_id,name,normalized_name) VALUES (%s,%s,NULL,'','') ON CONFLICT DO NOTHING",(demo_id(n+1),demo_id(n)))
    for w,u,role in [(10,1,'OWNER'),(10,2,'EDITOR'),(10,3,'VIEWER'),(20,5,'OWNER')]:
        conn.execute('INSERT INTO memberships(workspace_id,principal_id,role) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING',(demo_id(w),demo_id(u),role))


if __name__ == "__main__":
    initialize()
