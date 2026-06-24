# Enterprise Readiness

This document tracks the Agent platform's production-readiness work. It separates demo features from capabilities expected in an enterprise deployment.

## Implemented

- PostgreSQL primary database mode for the Agent platform.
- SQLite remains available for local development and smoke tests.
- Separate PostgreSQL connection profiles for runtime, migration, worker, and read-only diagnostics/backups.
- `AGENT_AUTO_MIGRATE=false` mode so production Web/Worker processes can start without DDL privileges.
- PostgreSQL-safe job claiming with `FOR UPDATE SKIP LOCKED`.
- Optional Redis workflow queue dispatch through `AGENT_QUEUE_BACKEND=redis`; the database remains the durable source of truth.
- Explicit schema migration registration through `schema_migrations`.
- Side-effect idempotency for ticket creation and email sending.
- Local external-ticket-service idempotency through the `Idempotency-Key` header.
- External side-effect outbox for ticket/email actions with pending/running/completed/failed states.
- Admin-triggered outbox retry for recovering failed ticket/email side effects.
- Dedicated Docker outbox dispatcher worker with due-event polling, max-attempt limits, and exponential retry backoff.
- Optional embedded outbox dispatcher through `AGENT_EMBEDDED_OUTBOX_DISPATCHER_ENABLED=true` for small single-process deployments.
- Database backup CLI for SQLite and PostgreSQL, plus explicit SQLite restore CLI.
- Tamper-evident audit hash chain with admin integrity verification.
- Revocable server-side auth sessions and `/api/auth/logout`.
- Optional OIDC/JWT bearer authentication with issuer, audience, JWKS, claim mapping, and group-to-role mapping.
- Browser-based OIDC authorization-code SSO with state cookie validation and local platform session issuance.
- Local Docker OIDC provider for end-to-end RS256/JWKS SSO demos without an external identity-provider dependency.
- SCIM-style user provisioning endpoints for enterprise directory lifecycle sync, including create, replace, patch, list, get, and disable-on-delete.
- Login failure audit and temporary account lockout controls.
- Tenant/workspace isolation foundation across users, requests, workflow runs, jobs, approvals, tickets, emails, external outbox, audit logs, multi-agent runs, trace replays, golden traces, and agent memory.
- OIDC tenant claim mapping through `AGENT_OIDC_TENANT_CLAIM`.
- Optional PostgreSQL row-level security policies through `AGENT_POSTGRES_RLS_ENABLED=true` for tenant-scoped business records and trace tables.
- Optional PostgreSQL RLS bypass role gate through `AGENT_POSTGRES_RLS_BYPASS_ROLE`; when configured, system-level bypass requires both `app.rls_bypass=on` and database role membership.
- Operational indexes for runs, jobs, approvals, tickets, emails, audit logs, and multi-agent traces.
- Migration CLI:

```powershell
.\.venv\Scripts\python.exe scripts\migrate.py
```

- Demo migration CLI:

```powershell
.\.venv\Scripts\python.exe scripts\migrate.py --seed --demo-users
```

- Production preflight CLI:

```powershell
.\.venv\Scripts\python.exe scripts\preflight.py
```

- Database backup CLI:

```powershell
.\.venv\Scripts\python.exe scripts\db_backup.py
```

- SQLite restore CLI:

```powershell
.\.venv\Scripts\python.exe scripts\db_restore_sqlite.py data\backups\YOUR_BACKUP.sqlite3 --yes
```

- PostgreSQL RLS setup and verification guide:

```text
docs/POSTGRES_RLS.md
```

- PostgreSQL role-separation setup guide:

```text
docs/POSTGRES_DB_ROLES.md
```

- Runtime readiness endpoint:

```text
GET /api/readiness
```

- Admin operational endpoints:

```text
GET /api/admin/preflight
GET /api/admin/schema-migrations
GET /api/admin/external-outbox
POST /api/admin/external-outbox/{outbox_id}/retry
POST /api/admin/external-outbox/retry-failed
POST /api/admin/external-outbox/dispatch-due
GET /api/admin/security/login-attempts
GET /api/admin/audit/integrity
GET /api/admin/retention/plan
POST /api/admin/retention/apply
```

- Request ID and response timing headers.
- W3C `traceparent` request correlation with `X-Trace-ID`, `X-Span-ID`, and `X-Request-ID` response headers.
- Structured JSON logging through `AGENT_LOG_FORMAT=json` with request, trace, tenant, workflow run, job, and worker correlation fields.
- Optional OpenTelemetry trace export through `AGENT_OTEL_ENABLED=true` and OTLP HTTP.
- Production-style Docker demo enables OpenTelemetry console export by default so trace instrumentation is exercised locally.
- Optional CORS origin allowlist.
- Optional Trusted Host allowlist.
- Security response headers.
- Docker health checks use `/api/readiness`, including database readiness.
- Prometheus-compatible `/metrics` endpoint with optional authentication.
- Observability setup guide:

```text
docs/OBSERVABILITY.md
```

- Workflow queueing setup guide:

```text
docs/QUEUEING.md
```

- Enterprise SSO setup guide:

```text
docs/SSO_OIDC.md
```

- SCIM provisioning setup guide:

```text
docs/SCIM.md
```

- Docker SSO smoke test:

```powershell
python scripts\docker_oidc_smoke_test.py --base-url http://127.0.0.1:8010 --sso-user admin
```

- Configurable data-retention policies for audit logs, evaluation reports, external outbox records, generated emails, workflow jobs, and login-attempt history.
- Dedicated Docker retention worker for scheduled cleanup of enabled retention policies.
- GitHub Actions CI for compile checks, smoke tests, frontend build, migration/preflight, and compose config.
- Human-in-the-loop approvals with department-scoped visibility.
- Audit log, workflow trace, multi-agent trace, replay, golden trace diff, and evaluation reports.
- Real integration adapters for SMTP, generic HTTP ticketing, Jira, local external-ticket-service, and RAG.

## Required For A Real Production Launch

- Replace all placeholder secrets in `.env.hr-demo` or production env vars.
- Use `AGENT_ENV=production`.
- Use `AGENT_AUTH_REQUIRED=true`.
- Use a long random `AGENT_AUTH_TOKEN_SECRET`.
- Configure enterprise SSO with `AGENT_OIDC_ENABLED=true`, `AGENT_OIDC_BROWSER_LOGIN_ENABLED=true`, `AGENT_OIDC_ISSUER`, `AGENT_OIDC_AUDIENCE`, `AGENT_OIDC_JWKS_URL`, `AGENT_OIDC_AUTHORIZATION_URL`, `AGENT_OIDC_TOKEN_URL`, `AGENT_OIDC_CLIENT_ID`, `AGENT_OIDC_CLIENT_SECRET`, and RS256-compatible `AGENT_OIDC_ALGORITHMS`.
- Use `AGENT_OIDC_ADMIN_GROUPS` and `AGENT_OIDC_MANAGER_GROUPS` to map identity-provider groups to platform roles.
- Enable `AGENT_SCIM_ENABLED=true` with a long random `AGENT_SCIM_TOKEN` when users are managed by an enterprise directory.
- Enable `AGENT_TENANT_ISOLATION_ENABLED=true` when the platform is shared by multiple customers, departments, or HR demo workspaces.
- Set `AGENT_DEFAULT_TENANT_ID` for local/demo fallback users and map SSO users through `AGENT_OIDC_TENANT_CLAIM`.
- Use `AGENT_DB_BACKEND=postgres`.
- Use separate PostgreSQL users through `AGENT_DATABASE_URL`, `AGENT_MIGRATION_DATABASE_URL`, `AGENT_WORKER_DATABASE_URL`, and `AGENT_READONLY_DATABASE_URL`.
- Run migrations as a separate release step and set `AGENT_AUTO_MIGRATE=false` on Web/Worker processes.
- Enable `AGENT_POSTGRES_RLS_ENABLED=true` with PostgreSQL for defense-in-depth tenant isolation.
- Create a dedicated PostgreSQL role for operational bypass, grant it only to trusted migration/worker users, and set `AGENT_POSTGRES_RLS_BYPASS_ROLE` to that role name.
- Configure backups and restore drills for PostgreSQL.
- Configure TLS through a reverse proxy or managed load balancer.
- Pin `AGENT_CORS_ORIGINS` to approved frontend URLs.
- Pin `AGENT_TRUSTED_HOSTS` when using known domains.
- Use `AGENT_METRICS_AUTH_REQUIRED=true` or expose `/metrics` only on a private network.
- Keep `AGENT_TRACEPARENT_ENABLED=true` so web requests, API calls, logs, and external gateways can be correlated.
- Use `AGENT_LOG_FORMAT=json` in shared environments.
- Configure `AGENT_OTEL_ENABLED=true` with `OTEL_EXPORTER_OTLP_ENDPOINT` for distributed tracing.
- Use `AGENT_QUEUE_BACKEND=redis` and `AGENT_REDIS_URL` for horizontally scalable worker dispatch.
- Enable `AGENT_OUTBOX_DISPATCHER_ENABLED=true` for automatic recovery of due external side effects, and run a dedicated dispatcher worker in production-style Docker stacks. Use `AGENT_EMBEDDED_OUTBOX_DISPATCHER_ENABLED=true` only for small single-process deployments.
- Configure retention windows with `AGENT_AUDIT_RETENTION_DAYS`, `AGENT_EXTERNAL_OUTBOX_RETENTION_DAYS`, `AGENT_EMAIL_RETENTION_DAYS`, `AGENT_WORKFLOW_JOB_RETENTION_DAYS`, and `AGENT_LOGIN_ATTEMPT_RETENTION_DAYS`.
- Configure SMTP allowlist or a dedicated mail provider sandbox before real sends.
- Configure ticket integration token rotation and least-privilege access.
- Run:

```powershell
.\.venv\Scripts\python.exe scripts\migrate.py
.\.venv\Scripts\python.exe scripts\preflight.py
.\.venv\Scripts\python.exe scripts\db_backup.py
```

- Review and apply retention before long-running demos or production handoff:

```powershell
Invoke-RestMethod http://127.0.0.1:8010/api/admin/retention/plan
Invoke-RestMethod -Method Post http://127.0.0.1:8010/api/admin/retention/apply
```

- Verify audit log integrity during operational checks:

```powershell
Invoke-RestMethod http://127.0.0.1:8010/api/admin/audit/integrity
```

## Next Enterprise Hardening Steps

- Extend SCIM with group resources and manager relationships if the enterprise identity provider requires them.
- Add managed queue adapters such as AWS SQS, Azure Service Bus, RabbitMQ, or Celery/RQ for environments that do not standardize on Redis.
- Add centralized log shipping, alert rules, and dashboards for HTTP errors, queue lag, outbox failures, and approval SLA breaches.
- Add deeper OpenTelemetry instrumentation for outbound HTTP integrations and database queries.
- Export audit hash anchors to external immutable storage.
- Add distributed leases for multiple active outbox dispatcher replicas.
- Add archive/export storage for long-term workflow trace and business record retention before deletion.
- Add tenant-aware object storage namespaces for exported traces, backups, and long-term archives.
- Add Alembic-style forward migrations for future schema changes.
- Extend CI with coverage thresholds, dependency vulnerability scanning, and container image scanning.
- Add encrypted secret management through cloud secret managers or Vault.
- Add formal disaster recovery runbooks with RPO/RTO targets and scheduled restore drills.
