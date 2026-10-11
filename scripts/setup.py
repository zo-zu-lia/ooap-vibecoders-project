#!/usr/bin/env python3
"""Create private local configuration. Existing secrets are never overwritten."""
import json
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
os.chdir(ROOT)
os.umask(0o077)
local = ROOT / '.local'
local.mkdir(exist_ok=True, mode=0o700)
(local / 'dex-data').mkdir(exist_ok=True, mode=0o700)
(local / 'metrics').mkdir(exist_ok=True, mode=0o755)
(local / 'metrics').chmod(0o755)
if (ROOT / '.env').exists():
    if not (local / 'dex.yaml').exists():
        sys.exit('Existing .env but missing Dex configuration; restore .local/dex.yaml before starting.')
    existing=dict(line.split('=',1) for line in (ROOT/'.env').read_text().splitlines() if line and not line.startswith('#'))
    if 'APP_DB_PASSWORD' not in existing:
        existing['APP_DB_PASSWORD']=secrets.token_hex(24)
        existing['MIGRATION_DATABASE_URL']=existing['DATABASE_URL']
        existing['DATABASE_URL']=f"postgresql://fileflow_app:{existing['APP_DB_PASSWORD']}@postgres:5432/fileflow"
        (ROOT/'.env').write_text('\n'.join(f'{k}={v}' for k,v in existing.items())+'\n')
    print('Existing local secrets preserved. Credentials: .local/credentials.txt')
    sys.exit(0)
try:
    import bcrypt
except ImportError:
    sys.exit('Install setup dependency first: python3 -m pip install bcrypt')

pg = secrets.token_hex(24)
app_db = secrets.token_hex(24)
s3 = secrets.token_hex(24)
oidc = secrets.token_hex(32)
app = secrets.token_hex(32)
password = secrets.token_urlsafe(18)
env = {
    'POSTGRES_PASSWORD': pg,
    'APP_DB_PASSWORD': app_db,
    'MIGRATION_DATABASE_URL': f'postgresql://fileflow:{pg}@postgres:5432/fileflow',
    'DATABASE_URL': f'postgresql://fileflow_app:{app_db}@postgres:5432/fileflow',
    'APP_ORIGIN': 'http://localhost:8080',
    'APP_SECRET': app,
    'COOKIE_SECURE': 'false',
    'LOCAL_DEMO': 'true',
    'OIDC_ISSUER': 'http://localhost:8080/dex',
    'OIDC_INTERNAL_URL': 'http://dex:5556/dex',
    'OIDC_CLIENT_ID': 'fileflow',
    'OIDC_CLIENT_SECRET': oidc,
    'S3_ENDPOINT': 'http://storage:9000',
    'S3_PUBLIC_ENDPOINT': 'http://localhost:9000',
    'S3_ACCESS_KEY': 'fileflow-local',
    'S3_SECRET_KEY': s3,
    'S3_BUCKET': 'fileflow',
    'S3_REGION': 'us-east-1',
    'CLAMAV_HOST': 'antivirus',
    'CLAMAV_PORT': '3310',
}
config = {
    'issuer': env['OIDC_ISSUER'],
    'storage': {'type': 'sqlite3', 'config': {'file': '/var/dex/dex.db'}},
    'web': {'http': '0.0.0.0:5556'},
    'oauth2': {'skipApprovalScreen': True},
    'enablePasswordDB': True,
    'staticClients': [{'id': 'fileflow', 'secret': oidc, 'name': 'FileFlow',
                       'redirectURIs': ['http://localhost:8080/auth/callback']}],
    'staticPasswords': [],
}
roles = ['owner', 'editor', 'viewer', 'security', 'finance']
for i, role in enumerate(roles, 1):
    config['staticPasswords'].append({
        'email': f'{role}@fileflow.local', 'username': role.title(),
        'userID': f'00000000-0000-4000-8000-{i:012}',
        'hash': bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode(),
    })
(local / 'dex.yaml').write_text(json.dumps(config, indent=2) + '\n')
(local / 'credentials.txt').write_text(
    'FileFlow local-only accounts\n\n' + '\n'.join(f'{r}@fileflow.local' for r in roles)
    + '\n\nPassword for these accounts: ' + password + '\n'
)
(local / 'test-credentials.json').write_text(json.dumps({'password': password}))
(ROOT / '.env').write_text('\n'.join(f'{k}={v}' for k, v in env.items()) + '\n')
print('Created private local configuration. Sign-in credentials: .local/credentials.txt')

