# PostgreSQL Role Separation

This guide describes a production-style database permission split for the Agent platform.

Local demos can keep using one PostgreSQL user. Production deployments should use separate users for runtime traffic, schema migration, background workers, and read-only diagnostics/backups.

## Connection Profiles

```env
AGENT_DB_BACKEND=postgres
AGENT_DATABASE_URL=postgresql://agent_runtime:CHANGE_ME@db.example.com:5432/agent
AGENT_MIGRATION_DATABASE_URL=postgresql://agent_migrator:CHANGE_ME@db.example.com:5432/agent
AGENT_WORKER_DATABASE_URL=postgresql://agent_worker:CHANGE_ME@db.example.com:5432/agent
AGENT_READONLY_DATABASE_URL=postgresql://agent_readonly:CHANGE_ME@db.example.com:5432/agent
AGENT_POSTGRES_SCHEMA=agent_app
AGENT_AUTO_MIGRATE=false
```

- `AGENT_DATABASE_URL`: Web/API runtime. Business CRUD only.
- `AGENT_MIGRATION_DATABASE_URL`: release-time schema creation, schema changes, seed data, and emergency reset.
- `AGENT_WORKER_DATABASE_URL`: background job and outbox worker processing.
- `AGENT_READONLY_DATABASE_URL`: readiness checks, backups, diagnostics, and read-only operational reporting.

## Initial Role Setup

Run this as a PostgreSQL admin user. Replace passwords and database names before use.

```sql
CREATE ROLE agent_runtime LOGIN PASSWORD 'CHANGE_ME_RUNTIME';
CREATE ROLE agent_migrator LOGIN PASSWORD 'CHANGE_ME_MIGRATOR';
CREATE ROLE agent_worker LOGIN PASSWORD 'CHANGE_ME_WORKER';
CREATE ROLE agent_readonly LOGIN PASSWORD 'CHANGE_ME_READONLY';

GRANT CONNECT ON DATABASE agent TO agent_runtime, agent_migrator, agent_worker, agent_readonly;
GRANT CREATE ON DATABASE agent TO agent_migrator;
```

Run the migration with the migrator DSN:

```powershell
$env:AGENT_DB_BACKEND="postgres"
$env:AGENT_MIGRATION_DATABASE_URL="postgresql://agent_migrator:CHANGE_ME_MIGRATOR@db.example.com:5432/agent"
$env:AGENT_DATABASE_URL="postgresql://agent_runtime:CHANGE_ME_RUNTIME@db.example.com:5432/agent"
$env:AGENT_POSTGRES_SCHEMA="agent_app"
.\.venv\Scripts\python.exe scripts\migrate.py --seed --demo-users
```

Then grant table permissions:

```sql
GRANT USAGE ON SCHEMA agent_app TO agent_runtime, agent_worker, agent_readonly;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA agent_app TO agent_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA agent_app TO agent_worker;
GRANT SELECT ON ALL TABLES IN SCHEMA agent_app TO agent_readonly;

ALTER DEFAULT PRIVILEGES FOR ROLE agent_migrator IN SCHEMA agent_app
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO agent_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE agent_migrator IN SCHEMA agent_app
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO agent_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE agent_migrator IN SCHEMA agent_app
  GRANT SELECT ON TABLES TO agent_readonly;
```

## RLS Bypass Role

If PostgreSQL RLS is enabled, configure a dedicated bypass role and grant it only to processes that are allowed to perform system-level cross-tenant maintenance.

```sql
CREATE ROLE agent_rls_bypass;
GRANT agent_rls_bypass TO agent_migrator;
GRANT agent_rls_bypass TO agent_worker;
```

Then set:

```env
AGENT_POSTGRES_RLS_ENABLED=true
AGENT_POSTGRES_RLS_BYPASS_ROLE=agent_rls_bypass
```

Do not grant `agent_rls_bypass` to analytics/read-only users. Grant it to the Web runtime only if that runtime also hosts trusted system jobs such as an in-process outbox dispatcher.

## Verification

```powershell
.\.venv\Scripts\python.exe scripts\migrate.py
.\.venv\Scripts\python.exe scripts\preflight.py
.\.venv\Scripts\python.exe scripts\postgres_rls_smoke_test.py
.\.venv\Scripts\python.exe scripts\db_backup.py
```

`scripts\preflight.py` prints whether runtime, migration, worker, and readonly connection profiles are configured.
