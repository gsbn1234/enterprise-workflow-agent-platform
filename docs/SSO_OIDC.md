# OIDC / Enterprise SSO

The Agent platform supports two OIDC modes:

- Bearer-token validation for API gateways and service-to-service calls.
- Browser authorization-code login for enterprise SSO.

## Browser Login Configuration

Register this redirect URI in your identity provider:

```text
https://YOUR_AGENT_DOMAIN/api/auth/oidc/callback
```

Then configure:

```env
AGENT_AUTH_REQUIRED=true
AGENT_OIDC_ENABLED=true
AGENT_OIDC_BROWSER_LOGIN_ENABLED=true
AGENT_OIDC_ISSUER=https://idp.example.com
AGENT_OIDC_AUDIENCE=agent-platform
AGENT_OIDC_JWKS_URL=https://idp.example.com/.well-known/jwks.json
AGENT_OIDC_AUTHORIZATION_URL=https://idp.example.com/oauth2/v1/authorize
AGENT_OIDC_TOKEN_URL=https://idp.example.com/oauth2/v1/token
AGENT_OIDC_CLIENT_ID=agent-platform
AGENT_OIDC_CLIENT_SECRET=CHANGE_ME
AGENT_OIDC_REDIRECT_URI=https://YOUR_AGENT_DOMAIN/api/auth/oidc/callback
AGENT_OIDC_SCOPES=openid,profile,email
AGENT_OIDC_ALGORITHMS=RS256
```

If `AGENT_OIDC_REDIRECT_URI` is empty, the platform derives it from `AGENT_PUBLIC_BASE_URL`.

## Claim Mapping

```env
AGENT_OIDC_SUB_CLAIM=sub
AGENT_OIDC_DISPLAY_NAME_CLAIM=name
AGENT_OIDC_EMAIL_CLAIM=email
AGENT_OIDC_DEPARTMENT_CLAIM=department
AGENT_OIDC_TENANT_CLAIM=tenant_id
AGENT_OIDC_GROUPS_CLAIM=groups
AGENT_OIDC_ADMIN_GROUPS=agent-admins
AGENT_OIDC_MANAGER_GROUPS=agent-approvers
AGENT_OIDC_DEFAULT_ROLE=employee
AGENT_OIDC_DEFAULT_DEPARTMENT=Operations
```

The platform maps OIDC users into local user records with an `oidc_` id. Group claims map users to `admin`, `manager`, or `employee`.

## Docker Demo Provider

The production-style Docker stack includes a local OIDC provider for HR/demo reviews:

```text
http://127.0.0.1:8030
```

It implements authorization-code login, one-time authorization codes, RS256 `id_token` signing, and a JWKS endpoint. It is useful for proving the full SSO flow without creating an Okta, Azure AD, Google Workspace, or Auth0 tenant.

Default demo identities:

- `Local SSO Admin`: maps to the `agent-admins` group and becomes an Agent admin.
- `Local SSO Manager`: maps to the `agent-approvers` group and becomes an Agent manager.
- `Local SSO Alice`: maps to the default employee role.

Docker verification:

```powershell
python scripts\docker_oidc_smoke_test.py --base-url http://127.0.0.1:8010 --sso-user admin
```

For real production, keep the Agent-side OIDC settings but point them to your enterprise IdP and remove the local demo provider from the deployment path.

## Runtime Flow

1. The login page calls `/api/auth/oidc/start`.
2. The backend sets an HttpOnly state cookie and redirects to the IdP authorization endpoint.
3. The IdP redirects back to `/api/auth/oidc/callback`.
4. The backend verifies state, exchanges the code for an `id_token`, validates the token, syncs the user, and issues a local platform access token.
5. The callback page stores the local token in `sessionStorage` and redirects back to the requested page.

## Verification

```powershell
.\.venv\Scripts\python.exe scripts\oidc_smoke_test.py
.\.venv\Scripts\python.exe scripts\oidc_browser_smoke_test.py
.\.venv\Scripts\python.exe scripts\preflight.py
```

For production, `scripts\preflight.py` warns when OIDC is disabled, browser SSO is disabled, or required OIDC fields are missing.
