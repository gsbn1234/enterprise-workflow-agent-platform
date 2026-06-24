# Enterprise Agent Demo Playbook

Use this playbook to demonstrate the Agent as an enterprise workflow system, not just a chatbot.

## Docker startup

Use the production-style Docker Compose stack when you want to demonstrate the Agent together with PostgreSQL, Redis, the RAG service, local enterprise SSO, and the local external ticket system.

If Docker Desktop fails to build from Chinese project paths, create ASCII junctions once:

```powershell
New-Item -ItemType Junction -Path F:\VScode-project\agent-platform-docker -Target "F:\VScode-project\企业业务流程自动化 Agent 平台"
New-Item -ItemType Junction -Path F:\VScode-project\rag-system-docker -Target "F:\VScode-project\企业知识库 RAG 系统"
```

Start the full Docker demo stack:

```powershell
cd F:\VScode-project\agent-platform-docker
$env:AGENT_BUILD_CONTEXT="F:/VScode-project/agent-platform-docker"
$env:RAG_BUILD_CONTEXT="F:/VScode-project/rag-system-docker"
python scripts\bootstrap_docker_env.py
docker compose -f docker-compose.prod.yml up -d agent-postgres
docker compose -f docker-compose.prod.yml exec -T agent-postgres psql -U agent -d agent -v ON_ERROR_STOP=1 -c 'DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = ''agent_rls_bypass'') THEN CREATE ROLE agent_rls_bypass; END IF; END $$; GRANT agent_rls_bypass TO agent;'
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
```

Expected production-style services:

- `workflow-agent`: FastAPI web/API and React UI.
- `workflow-worker`: Redis-backed async workflow worker.
- `workflow-outbox-dispatcher`: automatic retry/dispatch for ticket and email side effects.
- `workflow-retention-worker`: scheduled cleanup for audit/email/outbox/job/login history.
- `agent-postgres`, `agent-redis`, `rag-postgres`, `rag-app`, `external-ticket-service`, `local-oidc-provider`.

Docker demo URLs:

- Agent client: `http://127.0.0.1:8010`
- Agent admin console: `http://127.0.0.1:8010/admin`
- External ticket desk: `http://127.0.0.1:8020`
- Local enterprise SSO provider: `http://127.0.0.1:8030`
- RAG API health: `http://127.0.0.1:8000/health`

Useful checks:

```powershell
Invoke-RestMethod http://127.0.0.1:8010/api/readiness
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8020/api/tickets
Invoke-RestMethod http://127.0.0.1:8030/health
python scripts\docker_smoke_test.py --base-url http://127.0.0.1:8010 --user-id admin --password AdminPass123
python scripts\docker_oidc_smoke_test.py --base-url http://127.0.0.1:8010 --sso-user admin
python scripts\docker_scim_smoke_test.py --base-url http://127.0.0.1:8010
```

For the Docker demo, `/api/readiness` should report `production_ready=true`, `blocking_count=0`, and an empty `warnings` list. The local Docker stack enables OpenTelemetry console export and a local RS256/JWKS OIDC provider by default.

Clean demo history before recording:

1. Log in to `http://127.0.0.1:8010/admin` as `admin / AdminPass123`.
2. Click `清理轨迹` to clear Agent runs, approvals, tickets, jobs, emails, outbox, and audit history.
3. Open `http://127.0.0.1:8020` and click `Clear Demo Data` to clear the external ticket desk.

## Demo flow

1. Open the Agent workspace: `http://127.0.0.1:8010`
2. Log in as an employee, for example `alice / AlicePass123`.
3. Pick one scenario and submit it.
4. Show the user result, execution progress, decision explanation, RAG evidence, and generated artifacts.
5. Open the admin console: `http://127.0.0.1:8010/admin`.
6. Log in as `admin / AdminPass123` for global view, or a department manager such as `security_manager / ManagerPass123`.
7. Review approvals, workflow trace, ticket artifacts, and audit records.
8. Open the external ticket desk: `http://127.0.0.1:8020`.
9. Show the ticket timeline, SLA label, owner/priority/status operations, policy evidence, and approval sync.

## SSO demo

1. Open `http://127.0.0.1:8010/login`.
2. Click `SSO`.
3. The browser opens the local enterprise SSO provider at `http://127.0.0.1:8030`.
4. Choose `Local SSO Admin`, `Local SSO Manager`, or `Local SSO Alice`.
5. The provider redirects back to the Agent callback, the Agent verifies the RS256 `id_token` through JWKS, syncs an `oidc_...` user, and stores a local platform session.
6. Open the admin console and show that the SSO admin has admin permissions, while SSO manager/employee accounts are scoped by role.

## SCIM demo

SCIM is a backend integration demo, not a button in the browser.

1. Run `python scripts\docker_scim_smoke_test.py --base-url http://127.0.0.1:8010`.
2. Explain that this simulates Okta/Azure AD/Google Workspace provisioning a user into the Agent platform.
3. Show the admin user list: the created user has a deterministic `scim_...` id, department, role, and tenant.
4. Explain that `PATCH active=false` and `DELETE` disable the account instead of deleting audit history.
5. Open audit logs and point out `user.external_upsert` and `user.disable` events.

## Scenarios

| Scenario | Input summary | What to show | Expected result |
| --- | --- | --- | --- |
| Refund complaint | Orbit Retail requests 800 RMB refund and email reply | Customer lookup, RAG policy, refund approval chain, proposed ticket, email draft | Waiting approval first; after approval, external ticket and email record are created |
| Security incident | Suspicious Acme China login and possible access leak | Security classification, blocked destructive actions, internal notification, audit | Waiting approval first; after approval, ticket moves to investigating |
| Procurement | Marketing requests 3600 RMB analytics software | Budget extraction, procurement policy, approval, proposed ticket | Waiting Procurement Manager approval; rejected requests do not create external tickets |
| Remote work | Alice requests one remote-work day | RAG policy evidence, low-risk auto execution | Completed automatically, ticket status approved |
| Access request | Sales requests customer data export permission | Access risk, identity/approval controls, blocked privileged access | Waiting Line Manager and IT Access approval |
| P1 incident | Core login outage affects enterprise customers | SLA policy, SRE owner, incident workflow | Waiting Incident Commander approval or investigating after approval |

## Permission demo

- `admin / AdminPass123`: can see and operate all approvals, users, audit, and tickets.
- `security_manager / ManagerPass123`: can approve Security approvals and operate Security tickets.
- `it_manager / ManagerPass123`: can approve IT Access approvals and operate IT Access tickets.
- `people_manager / ManagerPass123`: can operate People Ops tickets.
- `alice / AlicePass123`: can submit requests but cannot approve or operate admin workflows.

## Talk track

The key message: the Agent plans a workflow, retrieves enterprise knowledge, evaluates risk, calls tools, pauses for human approval when needed, and writes every business action into an external ticket system with a timeline and audit trail.
