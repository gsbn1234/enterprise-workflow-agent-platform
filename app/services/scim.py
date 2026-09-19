from __future__ import annotations

import hashlib
from typing import Any

from app.config import settings
from app.services.auth import get_user, list_users, public_user, set_user_disabled, upsert_external_user
from app.services.it.rbac import KNOWN_ROLES


SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_ENTERPRISE_SCHEMA = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
SCIM_AGENT_SCHEMA = "urn:agent:params:scim:schemas:extension:workflow:2.0:User"


def scim_service_provider_config() -> dict[str, Any]:
    return {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
        "patch": {"supported": True},
        "bulk": {"supported": False},
        "filter": {"supported": False},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "Bearer Token",
                "description": "Use AGENT_SCIM_TOKEN as a bearer token.",
                "primary": True,
            }
        ],
    }


def scim_resource_types() -> dict[str, Any]:
    return {
        "schemas": [SCIM_LIST_SCHEMA],
        "totalResults": 1,
        "startIndex": 1,
        "itemsPerPage": 1,
        "Resources": [
            {
                "id": "User",
                "name": "User",
                "endpoint": "/scim/v2/Users",
                "schema": SCIM_USER_SCHEMA,
            }
        ],
    }


def scim_schemas() -> dict[str, Any]:
    return {
        "schemas": [SCIM_LIST_SCHEMA],
        "totalResults": 1,
        "startIndex": 1,
        "itemsPerPage": 1,
        "Resources": [
            {
                "id": SCIM_USER_SCHEMA,
                "name": "User",
                "description": "Enterprise Workflow Agent user profile.",
                "attributes": [
                    {"name": "userName", "type": "string", "required": True},
                    {"name": "displayName", "type": "string"},
                    {"name": "active", "type": "boolean"},
                ],
            }
        ],
    }


def list_scim_users(start_index: int = 1, count: int = 100) -> dict[str, Any]:
    users = list_users(limit=max(1, min(start_index + count + 50, 500)))
    start = max(1, start_index)
    size = max(1, min(count, 100))
    page = users[start - 1 : start - 1 + size]
    return {
        "schemas": [SCIM_LIST_SCHEMA],
        "totalResults": len(users),
        "startIndex": start,
        "itemsPerPage": len(page),
        "Resources": [to_scim_user(user) for user in page],
    }


def get_scim_user(user_id: str) -> dict[str, Any] | None:
    user = public_user(get_user(user_id))
    return to_scim_user(user) if user else None


def create_scim_user(payload: dict[str, Any]) -> dict[str, Any]:
    user_id = _scim_user_id(payload)
    return _upsert_from_payload(payload, user_id=user_id)


def replace_scim_user(user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _upsert_from_payload(payload, user_id=user_id)


def patch_scim_user(user_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    current = public_user(get_user(user_id))
    if not current:
        return None
    merged = {
        "userName": current["id"],
        "displayName": current["display_name"],
        "active": not current["disabled"],
        SCIM_ENTERPRISE_SCHEMA: {"department": current["department"]},
        SCIM_AGENT_SCHEMA: {"role": current["role"], "tenant_id": current["tenant_id"]},
    }
    for operation in payload.get("Operations") or []:
        op = str(operation.get("op") or "").lower()
        path = str(operation.get("path") or "").strip()
        value = operation.get("value")
        if op not in {"add", "replace"}:
            continue
        if path:
            _set_path_value(merged, path, value)
        elif isinstance(value, dict):
            _deep_update(merged, value)
    return _upsert_from_payload(merged, user_id=user_id)


def disable_scim_user(user_id: str) -> dict[str, Any] | None:
    user = set_user_disabled(user_id, True, actor="scim")
    return to_scim_user(user) if user else None


def to_scim_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemas": [SCIM_USER_SCHEMA, SCIM_ENTERPRISE_SCHEMA, SCIM_AGENT_SCHEMA],
        "id": user["id"],
        "userName": user["id"],
        "displayName": user["display_name"],
        "active": not user["disabled"],
        "name": {"formatted": user["display_name"]},
        SCIM_ENTERPRISE_SCHEMA: {"department": user["department"]},
        SCIM_AGENT_SCHEMA: {"role": user["role"], "tenant_id": user["tenant_id"]},
        "meta": {
            "resourceType": "User",
            "created": user["created_at"],
            "lastModified": user["updated_at"],
        },
    }


def _upsert_from_payload(payload: dict[str, Any], *, user_id: str) -> dict[str, Any]:
    display_name = str(payload.get("displayName") or (payload.get("name") or {}).get("formatted") or payload.get("userName") or user_id)
    enterprise = payload.get(SCIM_ENTERPRISE_SCHEMA) or {}
    agent = payload.get(SCIM_AGENT_SCHEMA) or {}
    department = str(enterprise.get("department") or payload.get("department") or "Operations")
    role = _role_from_payload(payload, agent)
    tenant_id = str(agent.get("tenant_id") or payload.get("tenant_id") or settings.default_tenant_id)
    active = bool(payload.get("active", True))
    user = upsert_external_user(
        user_id,
        display_name,
        department,
        role,
        tenant_id=tenant_id,
        disabled=not active,
        source="scim",
        actor="scim",
    )
    return to_scim_user(user)


def _role_from_payload(payload: dict[str, Any], agent_extension: dict[str, Any]) -> str:
    """Resolve the agent role from a SCIM payload.

    ``role`` and the namespaced agent extension exist only to carry our role, so
    an unrecognised value there is rejected — silently downgrading it to
    ``employee`` would hide an identity-provider misconfiguration. Values inside
    the standard ``roles`` list are treated as advisory instead: they routinely
    carry job functions ("Contractor", "Intern") that are not access roles, so
    unknown entries are skipped rather than fatal.
    """
    raw_role = str(agent_extension.get("role") or payload.get("role") or "").strip().lower()
    if raw_role:
        if raw_role in KNOWN_ROLES:
            return raw_role
        raise ValueError(f"Unknown SCIM role: {raw_role!r}.")
    for item in payload.get("roles") or []:
        value = str((item or {}).get("value") or "").strip().lower()
        if value in KNOWN_ROLES:
            return value
    return "employee"


def _scim_user_id(payload: dict[str, Any]) -> str:
    candidate = str(payload.get("id") or "").strip()
    if candidate.startswith("scim_"):
        return candidate[:80]
    external = str(payload.get("externalId") or payload.get("userName") or "").strip()
    if not external:
        raise ValueError("SCIM userName or externalId is required.")
    digest = hashlib.sha256(external.encode("utf-8")).hexdigest()[:24]
    return f"scim_{digest}"


def _set_path_value(payload: dict[str, Any], path: str, value: Any) -> None:
    normalized = path.replace(":", ".")
    if normalized in {"active", "displayName", "userName"}:
        payload[normalized] = value
    elif normalized.endswith(".department"):
        payload.setdefault(SCIM_ENTERPRISE_SCHEMA, {})["department"] = value
    elif normalized.endswith(".role"):
        payload.setdefault(SCIM_AGENT_SCHEMA, {})["role"] = value
    elif normalized.endswith(".tenant_id"):
        payload.setdefault(SCIM_AGENT_SCHEMA, {})["tenant_id"] = value


def _deep_update(target: dict[str, Any], source: dict[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value
