"""The one place an approved IT action is actually run.

Both callers share this module so the rule "nothing executes unless the
deterministic gate cleared it, and nothing that needed a human executes without
an approval id" is enforced once rather than twice:

* ``run_workflow``'s IT branch, for actions the gate marked ``auto_execute``.
* ``_human_approval_node``, after a human approved a ``require_approval`` action.

It is deliberately narrow. It does not decide anything — the decision was made
by ``app.services.it.risk_gate`` before this module was reached — and it does
not write to the ticket; the ticket state machine belongs to the caller. What it
owns is the execution itself and the single authoritative ``it.action_executed``
audit row, which is the only row in the platform that carries **why this ran**
(risk rule id) together with **who authorised it** (approval id) and **what
happened** (tool result). ``call_tool`` writes ``mcp.tool_call`` for every call
but cannot write those first two, because it has never heard of the gate or the
approval.

Four refusals are built in, and all four are fail-closed:

* no risk decision attached -> refuse. An action with no gate decision is not a
  cleared action.
* ``decision == deny`` -> refuse, and record it as ``it.action_denied``.
* ``executable is not True`` -> refuse. This is what stops a ``NO_KNOWLEDGE``
  resolution from being executed by a caller that only checked for approval:
  approval unlocks an executable action, it does not make an un-runnable one
  runnable.
* ``decision == require_approval`` with no ``approval_id`` -> refuse. A human
  said yes to *something*; without the id there is no evidence a human said
  anything, and the action is not run.

The tool that runs is derived from the deterministic ``ACTION_CLASSES`` table,
**not** from whatever the caller passed: ``action_type`` selects the class and
the class supplies the tool. A caller cannot pair ``diagnostic_read`` with
``restart_service``. Every call goes through ``call_tool``, so the role gate,
the resource check inside the tool and the ``mcp.tool_*`` audit rows all still
happen — an agent never reaches a Python function directly (requirement 八).
"""

from __future__ import annotations

import time
from typing import Any

from app.db import get_connection
from app.services.audit import record_audit
from app.services.it.actions import IT_SERVICE_ACCOUNT, LAYER_RISK_GATE
from app.services.it.risk_gate import (
    ACTION_CLASSES,
    DECISION_DENY,
    DECISION_REQUIRE_APPROVAL,
)
from app.services.tools.registry import call_tool
from app.utils import compact_text


SOURCE = "multi_agent_it"

# Why an action was not run. The first four are the caller-visible contract;
# the rest name a specific guard. They are recorded verbatim on
# ``it.action_not_executed`` so one audit query answers "why didn't it run".
NOT_EXECUTED_ALREADY_EXECUTED = "already_executed"
NOT_EXECUTED_APPROVAL_DENIED = "approval_denied"
NOT_EXECUTED_APPROVAL_REQUIRED = "approval_required"
NOT_EXECUTED_RISK_DENY = "risk_deny"
NOT_EXECUTED_NO_RISK_DECISION = "missing_risk_decision"
NOT_EXECUTED_UNKNOWN_ACTION = "unknown_action_type"
NOT_EXECUTED_TOOL_ACTION_MISMATCH = "tool_action_mismatch"
NOT_EXECUTED_NO_TOOL = "no_executable_tool"
NOT_EXECUTED_TOOL_ERROR = "tool_error"

# The gate's own rule ids, reused so "why not" is named in the gate's
# vocabulary rather than a second, drifting one invented here.
_GATE_REASONS: tuple[tuple[str, str], ...] = (
    ("no_knowledge_handoff", "no_knowledge_handoff"),
    ("request_more_information", "request_more_information"),
    ("no_executable_action", "no_executable_action"),
)


def execute_it_action(
    *,
    ticket_id: str | None,
    action_type: Any,
    tool_name: Any = None,
    arguments: dict[str, Any] | None = None,
    risk_decision: dict[str, Any] | None = None,
    approval_id: str | None = None,
    requested_by: str | None = None,
    workflow_run_id: str | None = None,
    multi_agent_run_id: str | None = None,
    prior_execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one IT action if — and only if — the guards above all pass.

    ``prior_execution`` is the ``it_execution`` state from an earlier pass of
    the same run. A self-correction loop can route back through the execution
    node a second time, and re-running an approved restart is not a retry, it is
    a second outage window; so a completed execution short-circuits here. This
    is separate from ``approvals.decision_applied``, which protects the decision
    and says nothing about the execution.
    """
    started = time.perf_counter()
    requested = str(action_type or "").strip().lower()
    # The requested tool is recorded for the mismatch check only; the tool that
    # runs always comes from the action class below.
    claimed_tool = str(tool_name or "").strip() or None
    call_arguments = dict(arguments or {})
    decision = dict(risk_decision or {})
    risk_rule_id = decision.get("rule_id")
    common = {
        "ticket_id": ticket_id,
        "action_type": requested or None,
        "tool_name": claimed_tool,
        "arguments": call_arguments,
        # Named explicitly as well as living in `arguments`, because the audit
        # question "which host was touched" should not require reading a blob.
        "asset_id": str(call_arguments.get("asset_id") or "").strip() or None,
        "approval_id": approval_id,
        "risk_rule_id": risk_rule_id,
        "risk_decision": decision.get("decision"),
        "workflow_run_id": workflow_run_id,
        "multi_agent_run_id": multi_agent_run_id,
        "requested_by": requested_by,
    }

    if isinstance(prior_execution, dict) and prior_execution.get("executed"):
        return _not_executed(
            NOT_EXECUTED_ALREADY_EXECUTED,
            common,
            prior=prior_execution.get("tool_name"),
            risk_class=prior_execution.get("risk_class"),
        )

    cls = ACTION_CLASSES.get(requested)
    if cls is None:
        return _not_executed(NOT_EXECUTED_UNKNOWN_ACTION, common)

    if not decision:
        # Fail closed: no gate decision means nobody cleared this.
        return _not_executed(NOT_EXECUTED_NO_RISK_DECISION, {**common, "risk_class": cls.risk_class})

    if decision.get("decision") == DECISION_DENY:
        record_audit(
            "it.action_denied",
            "ticket" if ticket_id else "it_action",
            ticket_id or requested,
            {
                "layer": LAYER_RISK_GATE,
                "action_type": requested,
                "risk_class": cls.risk_class,
                "rule_id": risk_rule_id,
                "reasons": list(decision.get("reasons") or []),
                "workflow_run_id": workflow_run_id,
                "multi_agent_run_id": multi_agent_run_id,
                "requested_by": requested_by,
            },
            actor=requested_by or "agent",
        )
        return _not_executed(NOT_EXECUTED_RISK_DENY, {**common, "risk_class": cls.risk_class})

    if decision.get("executable") is not True:
        return _not_executed(_not_executable_reason(decision), {**common, "risk_class": cls.risk_class})

    if decision.get("decision") == DECISION_REQUIRE_APPROVAL and not approval_id:
        return _not_executed(NOT_EXECUTED_APPROVAL_REQUIRED, {**common, "risk_class": cls.risk_class})

    if claimed_tool and claimed_tool != cls.tool_name:
        return _not_executed(
            NOT_EXECUTED_TOOL_ACTION_MISMATCH,
            {**common, "risk_class": cls.risk_class, "expected_tool": cls.tool_name},
        )
    if not cls.tool_name:
        return _not_executed(NOT_EXECUTED_NO_TOOL, {**common, "risk_class": cls.risk_class})

    target = _target_of(cls.tool_name, call_arguments)
    with_tool = {**common, "tool_name": cls.tool_name, "target": target, "risk_class": cls.risk_class}
    result = call_tool(
        cls.tool_name,
        call_arguments,
        actor=requested_by or IT_SERVICE_ACCOUNT["user_id"],
        source=SOURCE,
        auth_context=IT_SERVICE_ACCOUNT,
    )
    latency_ms = int((time.perf_counter() - started) * 1000)

    if not isinstance(result, dict) or result.get("error"):
        # The tool already recorded it.action_failed for a precondition it
        # refused, and call_tool recorded mcp.tool_error for a raise. This row
        # is the execution layer's own: the action did not happen.
        return _not_executed(
            NOT_EXECUTED_TOOL_ERROR,
            with_tool,
            error=str((result or {}).get("error") if isinstance(result, dict) else result),
            latency_ms=latency_ms,
        )

    record_audit(
        "it.action_executed",
        "ticket" if ticket_id else "it_action",
        ticket_id or (target or cls.tool_name),
        {
            **{key: value for key, value in with_tool.items() if key != "risk_decision"},
            "decision": decision.get("decision"),
            "risk_reasons": list(decision.get("reasons") or []),
            "environment": result.get("environment") or decision.get("environment"),
            "executed_by": IT_SERVICE_ACCOUNT["user_id"],
            "result_summary": _summarize(result),
            "latency_ms": latency_ms,
            "mode": result.get("mode"),
        },
        actor=IT_SERVICE_ACCOUNT["user_id"],
    )
    return {
        "executed": True,
        "reason": None,
        "result": result,
        "latency_ms": latency_ms,
        # Named here as well as on the audit row so the ticket chain can show
        # which identity performed the action without a second read of the
        # audit log. ``requested_by`` is already in ``with_tool``; the two are
        # deliberately separate fields, because the account that executes is
        # never the account that asked.
        "executed_by": IT_SERVICE_ACCOUNT["user_id"],
        **with_tool,
    }


def find_approved_approval_id(workflow_run_id: str) -> str | None:
    """The approval that authorised this workflow run, if a human granted one.

    Read back from the approvals table rather than threaded through the resume
    payload: the id that unlocks an action should be the row a human actually
    decided, and a value carried in a summary could be set by whoever assembled
    the summary. Returning ``None`` leaves ``execute_it_action`` to refuse for
    want of an approval.
    """
    if not workflow_run_id:
        return None
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id FROM approvals
            WHERE run_id = ? AND status = 'approved'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (workflow_run_id,),
        ).fetchone()
    return str(row["id"]) if row else None


def find_denied_approval_id(workflow_run_id: str) -> str | None:
    """The approval a human refused for this workflow run, if there is one.

    The counterpart to :func:`find_approved_approval_id` for the path where the
    answer was no. Without it the "why didn't it run" row names the ticket and
    the run but not the decision that stopped it, leaving the reviewer's refusal
    reachable only by joining through ``workflow_run_id``.
    """
    if not workflow_run_id:
        return None
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id FROM approvals
            WHERE run_id = ? AND status = 'denied'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (workflow_run_id,),
        ).fetchone()
    return str(row["id"]) if row else None


def record_it_action_skipped(
    *,
    ticket_id: str | None,
    action_type: Any,
    tool_name: Any = None,
    reason: str = NOT_EXECUTED_APPROVAL_DENIED,
    risk_decision: dict[str, Any] | None = None,
    approval_id: str | None = None,
    requested_by: str | None = None,
    workflow_run_id: str | None = None,
    multi_agent_run_id: str | None = None,
) -> dict[str, Any]:
    """Record an action that was authorised to be considered but will not run.

    Used for the post-approval path, where "will not run" is a fact the caller
    established (the human said no) rather than a guard inside
    :func:`execute_it_action`. Sharing ``it.action_not_executed`` keeps every
    "why didn't it run" answer in one event type.
    """
    decision = dict(risk_decision or {})
    return _not_executed(
        reason,
        {
            "ticket_id": ticket_id,
            "action_type": str(action_type or "").strip().lower() or None,
            "tool_name": str(tool_name or "").strip() or None,
            "approval_id": approval_id,
            "risk_rule_id": decision.get("rule_id"),
            "risk_decision": decision.get("decision"),
            "workflow_run_id": workflow_run_id,
            "multi_agent_run_id": multi_agent_run_id,
            "requested_by": requested_by,
        },
    )


def it_action_arguments(resolution: dict[str, Any] | None) -> dict[str, Any]:
    """The arguments the tool will be called with, taken from the resolution.

    Kept next to the execution path on purpose: this is the last point at which
    the argument set is still just data. Nothing here is inferred — the
    resolution agent derived these deterministically, and a missing one is
    simply missing, which the tool then refuses.
    """
    resolution = resolution or {}
    arguments = resolution.get("action_arguments")
    return dict(arguments) if isinstance(arguments, dict) else {}


def _not_executable_reason(decision: dict[str, Any]) -> str:
    """Name the refusal using the gate's own rules, not a second vocabulary."""
    reasons = [str(item) for item in (decision.get("reasons") or [])]
    for gate_reason, mapped in _GATE_REASONS:
        if gate_reason in reasons:
            return mapped
    return "risk_decision_not_executable"


def _not_executed(reason: str, detail: dict[str, Any], **extra: Any) -> dict[str, Any]:
    payload = {
        "executed": False,
        "reason": reason,
        "result": None,
        **detail,
        **extra,
    }
    record_audit(
        "it.action_not_executed",
        "ticket" if detail.get("ticket_id") else "it_action",
        detail.get("ticket_id") or detail.get("action_type") or "it_action",
        {
            "reason": reason,
            **{key: value for key, value in payload.items() if key not in {"executed", "result"}},
        },
        actor=detail.get("requested_by") or "agent",
    )
    return payload


def _target_of(tool_name: str, arguments: dict[str, Any]) -> str | None:
    """The row this action acts on, whichever noun the tool uses for it."""
    for key in ("asset_id", "employee_id", "ticket_id"):
        value = str(arguments.get(key) or "").strip()
        if value:
            return value
    resource = str(arguments.get("resource") or "").strip()
    return resource or None


def _summarize(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "keys": sorted(result.keys())[:20],
        "executed": result.get("executed"),
        "granted": result.get("granted"),
        "environment": result.get("environment"),
        "simulated": result.get("simulated"),
        "text": compact_text(str(result), 240),
    }
