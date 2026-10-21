CREATE TABLE IF NOT EXISTS app_metadata (key text PRIMARY KEY,value text NOT NULL);
CREATE TABLE IF NOT EXISTS users (
 id uuid PRIMARY KEY, email text NOT NULL UNIQUE, display_name text NOT NULL,
 status text NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','DISABLED')),
 security_admin boolean NOT NULL DEFAULT false, idp_issuer text, idp_subject text,
 identity_checked_at timestamptz, UNIQUE(idp_issuer,idp_subject)
);
CREATE TABLE IF NOT EXISTS principals (
 id uuid PRIMARY KEY, kind text NOT NULL CHECK(kind IN ('USER','GROUP')),
 user_id uuid UNIQUE REFERENCES users(id), idp_issuer text, idp_group_key text,
 CHECK((kind='USER' AND user_id IS NOT NULL AND idp_group_key IS NULL) OR
       (kind='GROUP' AND user_id IS NULL AND idp_group_key IS NOT NULL)),
 UNIQUE(idp_issuer,idp_group_key)
);
CREATE TABLE IF NOT EXISTS principal_members (
 group_principal_id uuid REFERENCES principals(id), user_id uuid REFERENCES users(id),
 synced_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(group_principal_id,user_id)
);
CREATE TABLE IF NOT EXISTS workspaces (
 id uuid PRIMARY KEY,name text NOT NULL,quota_bytes bigint NOT NULL CHECK(quota_bytes>0),
 used_bytes bigint NOT NULL DEFAULT 0 CHECK(used_bytes>=0),
 reserved_bytes bigint NOT NULL DEFAULT 0 CHECK(reserved_bytes>=0),
 retention_days integer NOT NULL DEFAULT 30 CHECK(retention_days BETWEEN 1 AND 365),
 acl_revision bigint NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS memberships (
 workspace_id uuid REFERENCES workspaces(id), principal_id uuid REFERENCES principals(id),
 role text NOT NULL CHECK(role IN ('VIEWER','EDITOR','OWNER')), PRIMARY KEY(workspace_id,principal_id)
);
CREATE TABLE IF NOT EXISTS folders (
 id uuid PRIMARY KEY,workspace_id uuid NOT NULL REFERENCES workspaces(id),parent_id uuid,
 name text NOT NULL,normalized_name text NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(), UNIQUE(id,workspace_id),
 FOREIGN KEY(parent_id,workspace_id) REFERENCES folders(id,workspace_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS folders_names ON folders(workspace_id,COALESCE(parent_id,'00000000-0000-0000-0000-000000000000'::uuid),normalized_name);
CREATE TABLE IF NOT EXISTS files (
 id uuid PRIMARY KEY,workspace_id uuid NOT NULL REFERENCES workspaces(id),folder_id uuid NOT NULL,
 name text NOT NULL,normalized_name text NOT NULL,latest_version_no integer NOT NULL DEFAULT 0,
 purge_state text NOT NULL DEFAULT 'ACTIVE' CHECK(purge_state IN ('ACTIVE','TRASHED','PURGING','PURGED')),
 deleted_at timestamptz,purge_at timestamptz,legal_hold boolean NOT NULL DEFAULT false,
 created_by uuid NOT NULL REFERENCES users(id),created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),FOREIGN KEY(folder_id,workspace_id) REFERENCES folders(id,workspace_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS files_active_names ON files(folder_id,normalized_name) WHERE purge_state='ACTIVE';
CREATE INDEX IF NOT EXISTS files_workspace ON files(workspace_id,purge_state,normalized_name,id);
CREATE TABLE IF NOT EXISTS storage_objects (
 id uuid PRIMARY KEY,workspace_id uuid NOT NULL REFERENCES workspaces(id),object_key text NOT NULL UNIQUE,
 size_bytes bigint NOT NULL CHECK(size_bytes>0),sha256 text NOT NULL,content_type text NOT NULL DEFAULT 'application/octet-stream',
 state text NOT NULL DEFAULT 'AVAILABLE',created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS upload_sessions (
 id uuid PRIMARY KEY,workspace_id uuid NOT NULL REFERENCES workspaces(id),folder_id uuid NOT NULL,
 file_id uuid REFERENCES files(id),actor_id uuid NOT NULL REFERENCES users(id),name text NOT NULL,
 normalized_name text NOT NULL,storage_upload_id text,object_key text NOT NULL UNIQUE,
 size_bytes bigint NOT NULL CHECK(size_bytes BETWEEN 1 AND 2147483648),sha256 text NOT NULL,content_type text NOT NULL,
 expected_version_no integer,state text NOT NULL DEFAULT 'INITIATED',part_size_bytes bigint NOT NULL,
 part_count integer NOT NULL,parts jsonb,reserved_bytes bigint NOT NULL DEFAULT 0,
 version_no integer,error_code text,created_at timestamptz NOT NULL DEFAULT now(),expires_at timestamptz NOT NULL,
 completion_key text,completion_hash text,complete_started_at timestamptz,init_key text,init_hash text,
 FOREIGN KEY(folder_id,workspace_id) REFERENCES folders(id,workspace_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uploads_init_key ON upload_sessions(actor_id,init_key) WHERE init_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS uploads_expiry ON upload_sessions(state,expires_at);
CREATE TABLE IF NOT EXISTS file_versions (
 file_id uuid REFERENCES files(id),version_no integer NOT NULL CHECK(version_no>0),object_id uuid NOT NULL REFERENCES storage_objects(id),
 scan_status text NOT NULL DEFAULT 'PENDING_SCAN' CHECK(scan_status IN ('PENDING_SCAN','CLEAN','REJECTED')),
 created_by uuid NOT NULL REFERENCES users(id),created_at timestamptz NOT NULL DEFAULT now(),upload_id uuid UNIQUE REFERENCES upload_sessions(id),
 PRIMARY KEY(file_id,version_no)
);
CREATE INDEX IF NOT EXISTS versions_download ON file_versions(file_id,scan_status,version_no);
CREATE TABLE IF NOT EXISTS file_permissions (
 file_id uuid REFERENCES files(id),user_id uuid REFERENCES users(id),role text NOT NULL CHECK(role IN ('VIEWER','EDITOR')),
 PRIMARY KEY(file_id,user_id)
);
CREATE TABLE IF NOT EXISTS share_links (
 id uuid PRIMARY KEY,file_id uuid NOT NULL REFERENCES files(id),recipient_user_id uuid NOT NULL REFERENCES users(id),
 created_by uuid NOT NULL REFERENCES users(id),secret_hash text NOT NULL UNIQUE,expires_at timestamptz NOT NULL,
 revoked_at timestamptz,created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS jobs (
 id uuid PRIMARY KEY,kind text NOT NULL,target_id uuid NOT NULL,upload_id uuid REFERENCES upload_sessions(id),
 state text NOT NULL DEFAULT 'QUEUED',attempts integer NOT NULL DEFAULT 0,lease_token uuid,lease_until timestamptz,
 next_run_at timestamptz NOT NULL DEFAULT now(),generation bigint NOT NULL DEFAULT 0,error_code text,
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),UNIQUE(kind,target_id,generation)
);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state,next_run_at);
CREATE TABLE IF NOT EXISTS sessions (
 id_hash text PRIMARY KEY,user_id uuid NOT NULL REFERENCES users(id),csrf_token text NOT NULL,
 refresh_token_encrypted text,created_at timestamptz NOT NULL DEFAULT now(),last_seen_at timestamptz NOT NULL DEFAULT now(),
 identity_checked_at timestamptz NOT NULL DEFAULT now(),expires_at timestamptz NOT NULL,revoked_at timestamptz
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS oidc_states (
 state_hash text PRIMARY KEY,nonce text NOT NULL,verifier_encrypted text NOT NULL,
 expires_at timestamptz NOT NULL,created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS idempotency_records (
 actor_id uuid REFERENCES users(id),operation text NOT NULL,key uuid NOT NULL,request_hash text NOT NULL,
 response jsonb,response_encrypted text,status integer NOT NULL,expires_at timestamptz NOT NULL DEFAULT now()+interval '24 hours',
 PRIMARY KEY(actor_id,operation,key)
);
CREATE TABLE IF NOT EXISTS audit_events (
 id uuid PRIMARY KEY,actor_id uuid REFERENCES users(id),action text NOT NULL,target_id uuid,
 workspace_id uuid REFERENCES workspaces(id),result text NOT NULL,request_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),effect_key text UNIQUE
);
CREATE INDEX IF NOT EXISTS audit_pagination ON audit_events(created_at,id);
CREATE INDEX IF NOT EXISTS audit_workspace ON audit_events(workspace_id,created_at);
CREATE TABLE IF NOT EXISTS rate_limits (bucket text PRIMARY KEY,window_start timestamptz NOT NULL,hits integer NOT NULL);
CREATE OR REPLACE FUNCTION deny_audit_change() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN RAISE EXCEPTION 'audit_events is append-only'; END; $$;
DROP TRIGGER IF EXISTS audit_append_only ON audit_events;
CREATE TRIGGER audit_append_only BEFORE UPDATE OR DELETE ON audit_events FOR EACH ROW EXECUTE FUNCTION deny_audit_change();
CREATE TABLE IF NOT EXISTS observability_scans (
 outcome text NOT NULL,size_bucket text NOT NULL,count bigint NOT NULL DEFAULT 0,total double precision NOT NULL DEFAULT 0,
 buckets jsonb NOT NULL,PRIMARY KEY(outcome,size_bucket)
);
CREATE TABLE IF NOT EXISTS observability_job_failures (
 kind text NOT NULL,reason text NOT NULL,count bigint NOT NULL DEFAULT 0,PRIMARY KEY(kind,reason)
);
INSERT INTO observability_job_failures(kind,reason,count)
 SELECT kind,reason,0 FROM unnest(ARRAY['SCAN','PURGE','CLEANUP','COMPLETE']) kind
 CROSS JOIN unnest(ARRAY['LEASE_EXPIRED','SCANNER_UNAVAILABLE','STORAGE_UNAVAILABLE','INTERNAL_ERROR','OTHER']) reason
 ON CONFLICT DO NOTHING;
CREATE OR REPLACE FUNCTION count_job_failure() RETURNS trigger LANGUAGE plpgsql AS $$
 BEGIN
  IF NEW.state='FAILED' AND OLD.state IS DISTINCT FROM 'FAILED' THEN
   INSERT INTO observability_job_failures(kind,reason,count)
    VALUES(NEW.kind,CASE WHEN NEW.error_code IN ('LEASE_EXPIRED','SCANNER_UNAVAILABLE','STORAGE_UNAVAILABLE','INTERNAL_ERROR') THEN NEW.error_code ELSE 'OTHER' END,1)
    ON CONFLICT(kind,reason) DO UPDATE SET count=observability_job_failures.count+1;
  END IF;
  RETURN NEW;
 END; $$;
DROP TRIGGER IF EXISTS job_failure_counter ON jobs;
CREATE TRIGGER job_failure_counter AFTER UPDATE ON jobs FOR EACH ROW EXECUTE FUNCTION count_job_failure();
CREATE TABLE IF NOT EXISTS analytics_events (
 id uuid PRIMARY KEY,event_name text NOT NULL CHECK(event_name IN ('activity.authorized','download.link_issued','share.resolve_attempted','share.resolve_succeeded','search.executed','version.complete_attempted','file.recover_attempted')),
 actor_key text,workspace_id uuid REFERENCES workspaces(id),resource_id uuid,version_no integer,
 operation_key text NOT NULL UNIQUE,result text NOT NULL,reason_code text,result_bucket text,
 occurred_at timestamptz NOT NULL DEFAULT now(),is_synthetic boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS analytics_events_period ON analytics_events(workspace_id,occurred_at,event_name);
CREATE INDEX IF NOT EXISTS analytics_events_resource ON analytics_events(resource_id,event_name,occurred_at);
CREATE TABLE IF NOT EXISTS analytics_daily (
 day date NOT NULL,scope text NOT NULL,metrics jsonb NOT NULL,updated_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(day,scope)
);
INSERT INTO app_metadata(key,value) VALUES ('analytics_started_at',now()::text) ON CONFLICT DO NOTHING;
