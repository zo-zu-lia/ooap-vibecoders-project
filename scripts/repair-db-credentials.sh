#!/bin/sh
# Reconcile this local deployment's database roles with its configured secrets.
# PostgreSQL initialization variables do not update roles in an existing volume.
set -eu
cd "$(dirname "$0")/.."

# Recreate only PostgreSQL if its environment changed, retaining its data volume.
./scripts/compose.sh up -d --no-deps --wait postgres
./scripts/compose.sh exec -T postgres sh -eu -c \
    'psql -X -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB"' <<'SQL'
BEGIN;
SET LOCAL log_statement = 'none';
SET LOCAL log_min_duration_statement = -1;
SET LOCAL log_min_error_statement = 'panic';
\getenv admin_user POSTGRES_USER
\getenv admin_password POSTGRES_PASSWORD
\getenv app_password APP_DB_PASSWORD
SELECT format('ALTER ROLE %I PASSWORD %L', :'admin_user', :'admin_password') \gexec
SELECT 'CREATE ROLE fileflow_app LOGIN'
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fileflow_app') \gexec
SELECT format('ALTER ROLE fileflow_app PASSWORD %L', :'app_password') \gexec
COMMIT;
SQL
echo 'Database role passwords now match the local configuration. Existing data is preserved.'
