# Local External Ticket Service

This is a standalone ticket desk for local demos. It is intentionally separate from the Agent database so the Agent calls it through the generic HTTP ticket adapter, just like it would call Jira, Zendesk, or an internal ticket API.

It supports:

- ticket creation with workflow type, category, risk level, owner, priority, approval chain, blocked actions, and knowledge evidence
- ticket status changes such as `open`, `waiting_approval`, `approved`, `rejected`, `waiting_customer`, and `investigating`
- an operational timeline for Agent sync events, approval decisions, owner changes, and internal comments
- searchable/filterable HTML pages for demo viewing
- dashboard statistics, SLA watch labels, owner reassignment, priority changes, and status changes
- JSON APIs for Agent integration and manual testing

Start it from the Agent project root:

```powershell
.\.venv\Scripts\python.exe -m uvicorn external_ticket_service.server:app --host 127.0.0.1 --port 8020
```

Configure the Agent:

```text
AGENT_TOOL_MODE=real
AGENT_TICKET_PROVIDER=http
TICKET_API_URL=http://127.0.0.1:8020/api/tickets
TICKET_HTTP_ID_FIELD=id
TICKET_HTTP_URL_FIELD=url
```

Open the ticket desk:

```text
http://127.0.0.1:8020
```

Useful pages and endpoints:

```text
GET  /                         HTML ticket list
GET  /tickets/{ticket_id}       HTML ticket detail and timeline
GET  /api/tickets               JSON ticket list
GET  /api/tickets/{ticket_id}   JSON ticket detail with events
POST /api/tickets               Create ticket
PATCH /api/tickets/{ticket_id}  Update status/owner/comment
POST /api/tickets/{ticket_id}/comments
POST /api/admin/clear           Clear demo ticket data
```

Example update payload:

```json
{
  "status": "waiting_approval",
  "comment": "Waiting for Customer Success Manager approval.",
  "actor": "agent",
  "approval_id": "appr_xxx",
  "agent_run_id": "run_xxx"
}
```
