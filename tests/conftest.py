"""Real OIDC and HTTP integration helpers. Never bypass authentication."""
import hashlib
import json
import os
from pathlib import Path
import time
import re
from uuid import uuid4
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx
import pytest
import yaml
import jsonschema

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = os.getenv('FILEFLOW_TEST_ORIGIN', 'http://localhost:8080')


def json_schema(value):
    if isinstance(value,list): return [json_schema(x) for x in value]
    if not isinstance(value,dict): return value
    result={k:json_schema(v) for k,v in value.items() if k!='nullable'}
    if value.get('nullable'):
        return {'anyOf':[result,{'type':'null'}]}
    return result


CONTRACT=json_schema(yaml.safe_load((ROOT/'docs/FileFlow_OpenAPI.yaml').read_text()))
COVERED=set()


def validate_contract(method,route,response):
    for path,operations in CONTRACT['paths'].items():
        pattern=re.sub(r'\{[^}]+\}', '[^/]+', path)
        if not re.fullmatch(pattern,route) or method.lower() not in operations: continue
        operation=operations[method.lower()]
        spec=operation['responses'].get(str(response.status_code))
        assert spec is not None, f'Undocumented response {response.status_code} on {method} {path}'
        resolver=jsonschema.RefResolver.from_schema(CONTRACT)
        if '$ref' in spec: spec=resolver.resolve(spec['$ref'])[1]
        media=response.headers.get('content-type','').split(';')[0]
        schema=spec.get('content',{}).get(media,{}).get('schema')
        if schema: jsonschema.Draft7Validator(schema,resolver=resolver).validate(response.json())
        COVERED.add((method.upper(),path))
        return


class Form(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = ''
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'form':
            self.action = a.get('action', '')
        if tag == 'input' and a.get('name'):
            self.fields[a['name']] = a.get('value', '')


class API:
    def __init__(self, role):
        self.client = httpx.Client(base_url=ORIGIN, follow_redirects=True, timeout=35)
        page = self.client.get('/auth/login')
        form = Form()
        form.feed(page.text)
        assert 'login' in form.fields, f'OIDC login form missing: status={page.status_code}'
        password = json.loads((ROOT / '.local/test-credentials.json').read_text())['password']
        form.fields.update(login=f'{role}@fileflow.local', password=password)
        response = self.client.post(urljoin(str(page.url), form.action), data=form.fields)
        assert response.status_code == 200, f'OIDC login failed: {response.status_code}'
        me = self.client.get('/api/v1/me')
        assert me.status_code == 200, f'OIDC session failed: {me.text}'
        self.me = me.json()
        self.client.headers.update({'X-CSRF-Token': self.me['csrfToken'], 'Origin': ORIGIN})

    def request(self, method, route, *, expected=200, key=None, **kwargs):
        headers = kwargs.pop('headers', {})
        if method.upper() == 'POST':
            headers.setdefault('Idempotency-Key', key or str(uuid4()))
        r = self.client.request(method, '/api/v1' + route, headers=headers, **kwargs)
        assert r.status_code == expected, f'{method} {route}: expected {expected}, got {r.status_code}: {r.text[:600]}'
        if expected >= 400:
            assert r.headers['content-type'].startswith('application/problem+json')
            assert r.json()['requestId']
        validate_contract(method,route,r)
        return r.json() if r.content else None

    def get(self, path, **kw):
        return self.request('GET', path, **kw)

    def post(self, path, **kw):
        return self.request('POST', path, **kw)

    def prepare(self, data=b'FileFlow integration document\n', *, file_id=None, expected_version=None, name=None, sha=None, folder=None):
        body = dict(sizeBytes=len(data), sha256=sha or hashlib.sha256(data).hexdigest(), contentType='text/plain')
        if file_id:
            route = f'/files/{file_id}/uploads'
            body['expectedVersionNo'] = expected_version
        else:
            route = f'/folders/{folder or "00000000-0000-4000-8000-000000000011"}/uploads'
            body['name'] = name or f'test-{uuid4().hex[:10]}.txt'
        session = self.post(route, json=body, expected=201)
        parts = []
        for i in range(session['partCount']):
            signed = self.get(f'/uploads/{session["uploadId"]}/parts/{i+1}/url')
            chunk = data[i*session['partSizeBytes']:(i+1)*session['partSizeBytes']]
            r = httpx.put(signed['url'], content=chunk, headers=signed['requiredHeaders'], timeout=60)
            assert r.status_code == 200, f'S3 PUT failed: {r.status_code}'
            parts.append(dict(partNumber=i+1, etag=r.headers['etag']))
        return session, {'parts': parts}

    def complete(self, session, parts, *, expected=202, key=None):
        return self.post(f'/uploads/{session["uploadId"]}/complete', json=parts, expected=expected, key=key)

    def wait_scan(self, session, status='READY'):
        deadline = time.monotonic()+90
        while time.monotonic() < deadline:
            result = self.get(f'/uploads/{session["uploadId"]}')
            if result['status'] in ('READY','REJECTED','CONFLICT','EXPIRED'):
                assert result['status'] == status, result
                return result
            time.sleep(1)
        pytest.fail('Scan worker did not finish within 90 seconds')

    def upload(self, data=b'FileFlow integration document\n', **kw):
        session, parts = self.prepare(data, **kw)
        self.complete(session, parts)
        return self.wait_scan(session)


@pytest.fixture(scope='session')
def accounts():
    clients = {role: API(role) for role in ('owner','editor','viewer','security','finance')}
    yield clients
    for api in clients.values():
        api.client.close()


def pytest_sessionfinish(session, exitstatus):
    (ROOT/'.local').mkdir(exist_ok=True)
    (ROOT/'.local/api-coverage.json').write_text(json.dumps(sorted(f'{method} {route}' for method,route in COVERED),indent=2))

