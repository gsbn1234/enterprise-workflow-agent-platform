# PostgreSQL RLS Tenant Isolation

This platform supports an optional PostgreSQL row-level security layer for tenant-scoped business records and trace tables.

For production database users and grants, see `docs/POSTGRES_DB_ROLES.md`.

## Configuration

For local demos, keep the role gate empty:

```env
AGENT_TENANT_ISOLATION_ENABLED=true
AGENT_DEFAULT_TENANT_ID=hr-demo
AGENT_POSTGRES_RLS_ENABLED=true
AGENT_POSTGRES_RLS_BYPASS_ROLE=
```

For stricter production deployments, create a PostgreSQL role and grant it only to trusted migration or worker users:

```sql
CREATE ROLE agent_rls_bypass;
GRANT agent_rls_bypass TO agent_migrator;
GRANT agent_rls_bypass TO agent_worker;
```

Then set:

```env
AGENT_POSTGRES_RLS_BYPASS_ROLE=agent_rls_bypass
```

With this role gate enabled, setting `app.rls_bypass=on` is not enough to bypass tenant isolation. The current database user must also be a member of `AGENT_POSTGRES_RLS_BYPASS_ROLE`.

## Verification

Run:

```powershell
.\.venv\Scripts\python.exe scripts\postgres_rls_smoke_test.py
.\.venv\Scripts\python.exe scripts\preflight.py
```

`postgres_rls_smoke_test.py` verifies tenant filtering, cross-tenant insert blocking, and the bypass role gate. If PostgreSQL is not reachable, it prints `agent_postgres_rls_smoke=skipped`.
