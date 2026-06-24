# SCIM User Provisioning

The Agent platform exposes a minimal SCIM 2.0-compatible provisioning surface for enterprise identity lifecycle sync.

## Configuration

```env
AGENT_SCIM_ENABLED=true
AGENT_SCIM_TOKEN=CHANGE_ME_LONG_RANDOM_TOKEN
```

All SCIM requests use bearer-token authentication:

```http
Authorization: Bearer CHANGE_ME_LONG_RANDOM_TOKEN
```

The Docker demo stack enables SCIM by default. Run this once before starting Docker so the local `.env` contains a strong token:

```powershell
python scripts\bootstrap_docker_env.py
```

## Endpoints

```text
GET    /scim/v2/ServiceProviderConfig
GET    /scim/v2/ResourceTypes
GET    /scim/v2/Schemas
GET    /scim/v2/Users
POST   /scim/v2/Users
GET    /scim/v2/Users/{user_id}
PUT    /scim/v2/Users/{user_id}
PATCH  /scim/v2/Users/{user_id}
DELETE /scim/v2/Users/{user_id}
```

`DELETE` disables the local user instead of physically deleting the account. This preserves audit history and prevents old tokens from continuing to authorize requests.

## Attribute Mapping

SCIM input:

```json
{
  "userName": "jane@example.com",
  "externalId": "directory-jane",
  "displayName": "Jane Example",
  "active": true,
  "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User": {
    "department": "Security"
  },
  "urn:agent:params:scim:schemas:extension:workflow:2.0:User": {
    "role": "manager",
    "tenant_id": "hr-demo"
  }
}
```

Agent user fields:

- `displayName` -> `users.display_name`
- enterprise `department` -> `users.department`
- Agent extension `role` -> `users.role` (`admin`, `manager`, or `employee`)
- Agent extension `tenant_id` -> `users.tenant_id`
- `active=false` -> `users.disabled=1`

SCIM-created users receive deterministic `scim_...` ids based on `externalId` or `userName`.

## Verification

Local service smoke test:

```powershell
.\.venv\Scripts\python.exe scripts\scim_smoke_test.py
```

Docker smoke test:

```powershell
.\.venv\Scripts\python.exe scripts\docker_scim_smoke_test.py --base-url http://127.0.0.1:8010
```

The Docker test reads `AGENT_SCIM_TOKEN` from `.env`, creates a SCIM user, patches `active=false`, and deletes the user by disabling it.
