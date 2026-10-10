#!/bin/sh
set -eu
# psql variables are quoted as SQL literals, never interpolated into shell code.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=app_password="$APP_DB_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE fileflow_app LOGIN PASSWORD %L', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='fileflow_app') \gexec
GRANT CONNECT ON DATABASE fileflow TO fileflow_app;
GRANT USAGE ON SCHEMA public TO fileflow_app;
ALTER DEFAULT PRIVILEGES FOR ROLE fileflow IN SCHEMA public GRANT SELECT,INSERT,UPDATE,DELETE ON TABLES TO fileflow_app;
SQL
