"""ASGI application and dependency health probes."""
import logging
import os
import time
from urllib.parse import urlsplit
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from . import analytics, core, observability, security, storage, uploads
from .config import settings
from .db import connection
from .errors import Problem, problem_response

log = logging.getLogger('fileflow')


@asynccontextmanager
async def lifespan(app):
    security.cipher()  # Fail startup for missing/weak encryption key.
    storage.initialize_bucket()
    analytics.start()
    yield
    analytics.stop()


app = FastAPI(title='FileFlow',version='1.0.0',lifespan=lifespan,docs_url=None,redoc_url=None)


@app.middleware('http')
async def security_headers(request, call_next):
    request.state.request_id = str(uuid4())
    started=time.perf_counter()
    status=500
    try:
        response = await call_next(request)
        status=response.status_code
    finally:
        if request.url.path.startswith('/api/v1/'):
            route=getattr(request.scope.get('route'),'path','unmatched')
            observability.observe_http(route,request.method,status,time.perf_counter()-started)
            analytics.observe_request(request,status)
    response.headers['X-Request-ID'] = request.state.request_id
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
    storage_origin = urlsplit(os.getenv('S3_PUBLIC_ENDPOINT','http://localhost:9000'))
    storage_origin = f'{storage_origin.scheme}://{storage_origin.netloc}'
    response.headers['Content-Security-Policy'] = f"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self' {storage_origin}; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    if settings.cookie_secure:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.exception_handler(Problem)
async def handle_problem(request, exc):
    request.state.problem_code=exc.code
    if exc.status in (401,403) and request.url.path.startswith('/api/'):
        # Independent append-only denial event: the domain transaction already rolled back.
        try:
            with connection() as conn:
                security.audit(conn,getattr(request.state,'actor_id',None),'ACCESS_DENIED',None,request.state.request_id,result=exc.code)
        except Exception:
            log.error('Denial audit unavailable request_id=%s',request.state.request_id)
    return problem_response(request,exc)


@app.exception_handler(RequestValidationError)
async def handle_validation(request,exc):
    fields=[dict(name='.'.join(map(str,e['loc'])),reason=e['msg']) for e in exc.errors()]
    return problem_response(request,Problem(400,'VALIDATION_FAILED','Request parameters do not match the API contract.',invalidParams=fields))


@app.exception_handler(HTTPException)
async def handle_http(request,exc):
    return problem_response(request,Problem(exc.status_code,'NOT_FOUND' if exc.status_code==404 else 'BAD_REQUEST','Route not found.' if exc.status_code==404 else 'Request could not be processed.'))


@app.exception_handler(psycopg.errors.UniqueViolation)
async def handle_unique(request,exc):
    return problem_response(request,Problem(409,'NAME_CONFLICT','An object or identity with that name already exists.'))


@app.exception_handler(psycopg.OperationalError)
@app.exception_handler(psycopg.errors.LockNotAvailable)
@app.exception_handler(BotoCoreError)
@app.exception_handler(ClientError)
async def handle_dependency(request,exc):
    log.warning('Dependency unavailable request_id=%s type=%s',request.state.request_id,type(exc).__name__)
    return problem_response(request,Problem(503,'DEPENDENCY_UNAVAILABLE','A required service is temporarily unavailable.'))


@app.exception_handler(Exception)
async def handle_unexpected(request,exc):
    # Do not log request headers, bodies, query strings, tokens, or exception values.
    log.error('Unexpected error request_id=%s type=%s',request.state.request_id,type(exc).__name__)
    return problem_response(request,Problem(500,'INTERNAL_ERROR','An unexpected error occurred. Use the request ID when reporting it.'))


@app.get('/health/live')
def live():
    return {'status':'ok'}


@app.get('/health/ready')
def ready():
    with connection() as conn:
        conn.execute('SELECT 1 FROM users LIMIT 1')
    storage.client().head_bucket(Bucket=storage.BUCKET)
    return {'status':'ready'}


@app.get('/metrics')
def metrics():
    return Response(observability.render_metrics(),media_type='text/plain; version=0.0.4')


app.include_router(security.router)
app.include_router(core.router)
app.include_router(analytics.router)
app.include_router(uploads.router,prefix='/api/v1')
static_dir = Path(__file__).with_name('static')
static_dir.mkdir(exist_ok=True)
app.mount('/static',StaticFiles(directory=static_dir),name='static')


@app.get('/')
def index():
    return FileResponse(static_dir/'index.html')
