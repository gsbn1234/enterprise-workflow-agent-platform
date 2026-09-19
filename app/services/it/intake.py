"""The Phase 1 IT intake loop.

    request -> guard -> triage -> employee/asset lookup -> ticket -> structured result

Every tool call goes through ``call_tool``, so this path is subject to exactly
the same role checks as the HTTP MCP route — there is no privileged shortcut.

Phase 1 deliberately stops after creating the ticket. Resolution, the risk gate,
human approval and tool execution are Phase 2; ``next_step`` names the hook they
will attach to rather than pretending they already ran. In particular the ticket
is left in ``open`` rather than ``waiting_approval``: no ``approvals`` row exists
yet, so a ``waiting_approval`` ticket would have no way out of that state.
"""

from __future__ import annotations

from typing import Any

from app.services.agent.planner import guard_objective
from app.services.audit import record_audit
from app.services.it.triage import TriageResult, classify
from app.services.tools.registry import call_tool
from app.utils import compact_text


IT_OWNER_DEPARTMENT = "IT"
IT_WORKFLOW_TYPE = "it_service_intake"

NEXT_STEP_NONE = "none"
NEXT_STEP_AWAITING_APPROVAL = "awaiting_human_approval"

MAX_TITLE_LENGTH = 200

ORIGINAL_REQUEST_MARKER = "Original request:"
"""Line ``_build_description`` writes immediately before the raw request text.

The description is the only place the untruncated words of the requester
survive — ``tickets.title`` is capped at ``MAX_TITLE_LENGTH``.
"""


def original_request_text(ticket: dict[str, Any] | None) -> str:
    """Recover the requester's own words from a Phase 1 intake ticket.

    Phase 2 replays the objective into the multi-agent graph, and replaying the
    generated ``title`` would lose everything past 200 characters. Falls back to
    the title for tickets that did not come from ``submit_it_request`` (e.g. a
    ticket created directly through the tool).
    """
    if not ticket:
        return ""
    description = str(ticket.get("description") or "")
    _, marker, tail = description.rpartition(ORIGINAL_REQUEST_MARKER)
    if marker and tail.strip():
        return tail.strip()
    return str(ticket.get("title") or "").strip()


def submit_it_request(objective: str, *, auth_context: Any, tenant_id: str | None = None) -> dict[str, Any]:
    """Run the minimal IT intake loop for one free-text request."""
    requester_id = _requester_id(auth_context)
    if not requester_id:
        raise ValueError("auth_context with a user_id is required for IT intake.")
    text = str(objective or "").strip()
    if not text:
        raise ValueError("objective is required.")

    guard = guard_objective(text)
    if not guard["allowed"]:
        record_audit(
            "it.request_refused",
            "it_request",
            None,
            {"requester": requester_id, "reason": guard["reason"], "objective": compact_text(text, 240)},
            actor=requester_id,
            tenant_id=tenant_id,
        )
        return {
            "ticket_id": None,
            "status": "refused",
            "requester": {"user_id": requester_id},
            "triage": None,
            "employee": None,
            "assets": [],
            "related_asset": None,
            "needs_approval": False,
            "missing_information": [],
            "guard": guard,
            "warnings": [],
            "evidence": [],
            "next_step": NEXT_STEP_NONE,
            "phase": 1,
        }

    triage = classify(text)
    warnings: list[str] = []
    evidence: list[dict[str, Any]] = []

    employee_result = call_tool(
        "get_employee", {"employee_id": requester_id}, actor=requester_id, source="it_intake", auth_context=auth_context
    )
    employee = employee_result.get("employee") if employee_result.get("found") else None
    evidence.append({"tool": "get_employee", "target": requester_id, "found": bool(employee)})
    if not employee:
        warnings.append("requester_not_in_directory")

    assets_result = call_tool(
        "get_user_assets", {}, actor=requester_id, source="it_intake", auth_context=auth_context
    )
    assets = assets_result.get("assets") or []
    evidence.append({"tool": "get_user_assets", "count": len(assets)})

    related_asset, asset_evidence = _resolve_related_asset(triage, auth_context, requester_id)
    if asset_evidence:
        evidence.append(asset_evidence)

    title = compact_text(text, MAX_TITLE_LENGTH) or "IT service request"
    description = _build_description(text, triage, employee, related_asset, warnings)
    ticket = call_tool(
        "create_ticket",
        {
            "title": title,
            "description": description,
            "priority": triage.priority,
            "owner_department": IT_OWNER_DEPARTMENT,
            "workflow_type": IT_WORKFLOW_TYPE,
            "category": triage.category,
            "risk_level": _risk_level(triage),
            "evidence": evidence,
            "agent_run_id": f"it-intake:{requester_id}",
            "tenant_id": tenant_id,
            "requester_user_id": requester_id,
            "it_category": triage.category,
            "service": triage.entities.get("service"),
            "asset_id": related_asset["id"] if related_asset else None,
            "environment": triage.entities.get("environment"),
            "triage": triage.to_dict(),
        },
        actor=requester_id,
        source="it_intake",
        auth_context=auth_context,
    )
    if ticket.get("error"):
        return {
            "ticket_id": None,
            "status": "failed",
            "requester": {"user_id": requester_id, "employee": employee},
            "triage": triage.to_dict(),
            "employee": employee,
            "assets": assets,
            "related_asset": related_asset,
            "needs_approval": triage.needs_approval,
            "missing_information": triage.missing_information,
            "guard": guard,
            "warnings": warnings + ["ticket_creation_failed"],
            "evidence": evidence,
            "ticket_error": ticket.get("error"),
            "next_step": NEXT_STEP_NONE,
            "phase": 1,
        }

    record_audit(
        "it.request_submitted",
        "ticket",
        ticket["id"],
        {
            "requester": requester_id,
            "intent": triage.intent,
            "category": triage.category,
            "priority": triage.priority,
            "needs_approval": triage.needs_approval,
            "asset_id": related_asset["id"] if related_asset else None,
        },
        actor=requester_id,
        tenant_id=tenant_id,
    )

    return {
        "ticket_id": ticket["id"],
        "status": ticket.get("status"),
        "requester": {"user_id": requester_id, "employee": employee},
        "triage": triage.to_dict(),
        "employee": employee,
        "assets": assets,
        "related_asset": related_asset,
        "needs_approval": triage.needs_approval,
        "missing_information": triage.missing_information,
        "guard": guard,
        "warnings": warnings,
        "evidence": evidence,
        "ticket": ticket,
        "next_step": NEXT_STEP_AWAITING_APPROVAL if triage.needs_approval else NEXT_STEP_NONE,
        "phase": 1,
        "phase_2_hook": "resolution_or_approval",
    }


def _resolve_related_asset(
    triage: TriageResult, auth_context: Any, requester_id: str
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Turn a triage service/resource code into a concrete asset, if one exists.

    The uppercase triage codes are the uppercase form of ``assets.asset_type``,
    so no mapping table is needed. ``DOCKER``/``NETWORK``/``EMAIL`` simply have no
    mock asset behind them and resolve to ``None``.
    """
    code = triage.entities.get("service") or triage.entities.get("resource")
    if not code:
        return None, None
    asset_type = str(code).lower()
    result = call_tool(
        "find_asset_by_type",
        {"asset_type": asset_type},
        actor=requester_id,
        source="it_intake",
        auth_context=auth_context,
    )
    if not result.get("found"):
        return None, {
            "tool": "find_asset_by_type",
            "asset_type": asset_type,
            "found": False,
            "reason": result.get("reason"),
        }
    return result["asset"], {"tool": "find_asset_by_type", "asset_type": asset_type, "found": True}


def _build_description(
    text: str,
    triage: TriageResult,
    employee: dict[str, Any] | None,
    related_asset: dict[str, Any] | None,
    warnings: list[str],
) -> str:
    lines = [
        "IT service intake (Phase 1: triage and record only).",
        "",
        f"Requester: {employee['name'] if employee else 'unknown'} ({employee['employee_id'] if employee else 'n/a'})",
        f"Department: {employee['department_name'] if employee else 'unknown'}",
        f"Intent: {triage.intent}",
        f"Category: {triage.category}",
        f"Priority: {triage.priority}",
    ]
    if related_asset:
        lines.append(f"Related asset: {related_asset['id']} ({related_asset['asset_type']}, {related_asset['environment']})")
    if triage.missing_information:
        lines.append(f"Missing information: {', '.join(triage.missing_information)}")
    if warnings:
        lines.append(f"Warnings: {', '.join(warnings)}")
    lines += ["", "Original request:", text]
    return "\n".join(lines)


def _risk_level(triage: TriageResult) -> str:
    if triage.priority in ("urgent", "high"):
        return "high"
    if triage.needs_approval:
        return "medium"
    return "low"


def _requester_id(auth_context: Any) -> str | None:
    user_id = getattr(auth_context, "user_id", None)
    if user_id is None and isinstance(auth_context, dict):
        user_id = auth_context.get("user_id")
    actor = str(user_id or "").strip()
    return actor or None
