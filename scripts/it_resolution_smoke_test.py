"""Phase 2 IT closed loop: Triage -> RAG -> Resolution -> Risk Gate -> execute / approve.

    "我的生产 Redis 连不上了"
      -> IT triage -> ticket read -> Knowledge RAG -> Resolution Agent
      -> deterministic risk gate -> auto-execute, or a human
      -> ticket state -> audit

Most of the cases below try to get around one of the three barriers — the role
check, the risk gate, or the human approval — and assert that they cannot. A
test that only walked the happy path would not tell "the gate works" apart from
"the gate was never in the way".

Nothing here contacts a real system: mock providers, no RAG service, and the
deterministic knowledge corpus seeded into ``knowledge_articles``. The LLM is
off, because a run that passes only when the model happens to agree with the
gate proves nothing about the gate.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "it_resolution_smoke_test.sqlite3")
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["AGENT_LLM_ENABLED"] = "false"
sys.path.insert(0, str(ROOT))

from app.db import get_connection, reset_database, rows_to_dicts, seed_it_knowledge  # noqa: E402
from app.main import IT_RESOLVABLE_STATUSES, _it_loop_state, it_resolve_request  # noqa: E402
from app.schemas import ITResolveRequest  # noqa: E402
from app.services.agent import decide_approval_and_resume, get_run_detail  # noqa: E402
from app.services.agent.retry import default_retry_policy  # noqa: E402
from app.services.audit import hydrate_audit_log, list_audit_logs  # noqa: E402
from app.services.auth import AuthContext, ensure_demo_users  # noqa: E402
from app.services.it.actions import IT_SERVICE_ACCOUNT  # noqa: E402
from app.services.it.intake import original_request_text, submit_it_request  # noqa: E402
from app.services.it.risk_gate import evaluate as evaluate_risk  # noqa: E402
from app.services.multi_agent import resume_multi_agent_for_workflow  # noqa: E402
from app.services.multi_agent.agents import ResolutionAgent  # noqa: E402
from app.services.tenancy import set_current_tenant_id  # noqa: E402
from app.services.tools.approvals import list_approvals  # noqa: E402
from app.services.tools.registry import call_tool  # noqa: E402
from app.services.tools.ticketing import get_ticket, list_ticket_events  # noqa: E402


EMPLOYEE = AuthContext(
    user_id="E002", display_name="李四", department="Engineering", role="employee", tenant_id="default"
)
IT_ADMIN = AuthContext(
    user_id="E003", display_name="王五", department="IT", role="it_admin", tenant_id="default"
)

# Three separate incident phrasings. ``submit_it_request`` is idempotent on the
# request text, so filing the same sentence twice returns the first ticket
# rather than a second one — each scenario needs its own ticket, and all three
# must still classify as a production incident on Redis.
REDIS_INCIDENT = "我的生产 Redis 连不上了"
REDIS_SLOW = "生产环境 Redis 异常，响应很慢"
REDIS_DOWN = "生产 Redis 连接失败，服务不可用"
CACHE_PRESSURE = "预发环境的服务器缓存压力很大，需要清理缓存"
PERMISSION_REQUEST = "我要申请生产数据库只读权限"
HANDBOOK_TITLE = "Redis 生产故障排查手册"

# Every registered IT action tool, so a case can assert that *none* of them ran
# without listing the four by hand in three different places.
IT_ACTION_TOOLS = ("diagnose_service", "flush_cache", "restart_service", "grant_permission")


def main() -> None:
    reset_database(seed=True)
    ensure_demo_users()
    # The resolve route normally gets this from its auth dependency; this script
    # calls the route function directly, so it sets the scope itself.
    set_current_tenant_id("default")

    assert IT_RESOLVABLE_STATUSES == frozenset({"open", "investigating"}), IT_RESOLVABLE_STATUSES
    assert HANDBOOK_TITLE in _knowledge_titles(), "the IT corpus is not seeded"
    assert list_audit_logs(limit=5, tenant_id="default"), "the public audit API must keep working"

    # 1 + 4. The scenario from the requirement, up to the approval it must stop at.
    incident = _redis_incident_reaches_risk_gate()
    _production_restart_requires_a_human(incident)
    # 2. No knowledge -> NO_KNOWLEDGE -> a human, never an invented answer.
    _no_knowledge_evidence_goes_to_a_human()
    # 3. A reversible action outside production runs on its own.
    _reversible_outside_production_runs_automatically()
    # 5. A permission grant always stops for a human.
    _permission_grant_requires_a_human()
    # 6. Approve -> the same thread resumes -> the action runs.
    _approved_action_resumes_and_executes(incident)
    # 7. Reject -> nothing runs, and the decision cannot be replayed.
    _rejected_action_never_executes()
    # 8. The gate itself cannot be bypassed, in code or through the graph.
    _risk_gate_rules()
    _denied_action_never_reaches_a_tool()
    # 10. A tool that fails is recorded, and is not retried into a second outage.
    _failed_tool_call_is_audited()
    _failed_action_fails_the_run_without_a_retry()

    print("it_resolution_smoke_test passed")
    print(f"it_tickets={len(_it_tickets())}")
    print(f"it_actions_executed={len(_audit('it.action_executed'))}")
    print(f"it_approvals_requested={len(_audit('it.approval_requested'))}")


# --------------------------------------------------------------------------- #
# 1. A Redis incident that finds its handbook, and stops where it must
# --------------------------------------------------------------------------- #


def _redis_incident_reaches_risk_gate() -> dict:
    """The requirement's scenario, end to end up to the approval it stops at."""
    intake = submit_it_request(REDIS_INCIDENT, auth_context=EMPLOYEE)
    assert intake["status"] == "open", intake
    assert intake["triage"]["intent"] == "IT_INCIDENT", intake["triage"]
    assert intake["triage"]["category"] == "REDIS", intake["triage"]
    assert intake["triage"]["priority"] == "urgent", intake["triage"]
    assert intake["triage"]["entities"]["environment"] == "production", intake["triage"]
    assert intake["related_asset"]["id"] == "REDIS-001", intake["related_asset"]
    ticket_id = intake["ticket_id"]

    # The route replays the reporter's own words, not the generated title. That
    # is what puts a real sentence into the retrieval query below.
    assert original_request_text(get_ticket(ticket_id)) == REDIS_INCIDENT

    response = it_resolve_request(ticket_id, ITResolveRequest(), auth_context=EMPLOYEE)

    # --- triage: the Phase 1 classifier, reused rather than re-run -----------
    triage = response["triage"]
    assert triage["intent"] == "IT_INCIDENT", triage
    assert triage["priority"] == "urgent", triage
    assert triage["source"] == "phase1_stored", triage
    assert triage["asset_id"] == "REDIS-001", triage
    assert triage["environment"] == "production", triage
    # The query the research nodes were given: the reporter's sentence, plus the
    # entity codes it did not already say. "Redis" is already in the sentence so
    # it is not repeated; "production" is the addition that makes the corpus
    # matchable, and it is asserted exactly rather than loosely.
    assert triage["research_query"] == f"{REDIS_INCIDENT} production", triage["research_query"]
    assert len(triage["research_query"]) <= 400, triage

    # --- retrieval reached the IT corpus through the existing knowledge tool --
    proposed = _audit_for("it.resolution_proposed", ticket_id)
    assert len(proposed) == 1, proposed
    cited = proposed[0]["detail"]["evidence"]
    assert cited, proposed[0]
    assert HANDBOOK_TITLE in [item["title"] for item in cited], cited
    assert cited[0]["source"] == "local_policy_db", cited
    assert cited[0]["article_id"], cited
    assert cited[0]["snippet"], cited

    # --- the resolution: structured, evidence-quoting, and only a proposal ----
    resolution = response["resolution"]
    assert resolution["status"] == "PROPOSED", resolution
    assert resolution["action_type"] == "service_restart", resolution
    assert resolution["proposed_action"] == "restart_service", resolution
    assert resolution["action_arguments"] == {"asset_id": "REDIS-001"}, resolution
    assert resolution["target"] == "REDIS-001", resolution
    assert resolution["environment"] == "production", resolution
    assert resolution["evidence_count"] >= 1, resolution
    assert resolution["evidence"][0]["title"] == cited[0]["title"], resolution["evidence"]
    assert resolution["mode"] == "deterministic", resolution
    # The diagnosis quotes the evidence rather than paraphrasing it, so a reader
    # can check it against the cited article.
    assert resolution["evidence"][0]["snippet"][:200] in resolution["diagnosis"], resolution["diagnosis"]
    # Confidence is reported and marked advisory; nothing downstream reads it.
    assert resolution["handoff_contract"]["advisory_only"] == ["confidence", "requires_approval"], resolution

    # --- nothing has run: a human has not decided yet ------------------------
    assert response["status"] == "waiting_approval", response["status"]
    assert response["ticket_status"] == "waiting_approval", response["ticket_status"]
    assert response["approval"], response
    assert response["execution"] is None, response["execution"]
    assert _audit_for("it.action_executed", ticket_id) == [], "nothing may run before the human decides"
    assert _audit_for("it.action_not_executed", ticket_id) == [], "nothing was refused either; it is pending"
    assert _mcp_calls(*IT_ACTION_TOOLS) == 0, "no IT tool may be reached before the human decides"

    assert _ticket_status_timeline(ticket_id) == ["open", "investigating", "waiting_approval"]
    # The same three transitions, seen through the API the demo reads them from.
    assert {event["to_status"] for event in list_ticket_events(ticket_id)} == {
        "open",
        "investigating",
        "waiting_approval",
    }, list_ticket_events(ticket_id)

    linked = _audit_for("it.ticket_linked", ticket_id)
    assert len(linked) == 1, linked
    assert linked[0]["detail"]["multi_agent_run_id"] == response["multi_agent_run_id"], linked[0]
    assert linked[0]["detail"]["workflow_run_id"] == response["workflow_run_id"], linked[0]

    return {
        "ticket_id": ticket_id,
        "run_id": response["multi_agent_run_id"],
        "workflow_run_id": response["workflow_run_id"],
        "approval_id": response["approval"]["id"],
        "risk_decision": response["risk_decision"],
    }


def _production_restart_requires_a_human(case: dict) -> None:
    """Requirement 六.3 and 六.5: a production restart always stops for a human."""
    decision = case["risk_decision"]
    assert decision["decision"] == "require_approval", decision
    assert decision["rule_id"] == "action_class_requires_approval:service_restart", decision
    assert decision["executable"] is True, decision
    assert decision["risk_class"] == "service_restart", decision
    assert decision["tool_name"] == "restart_service", decision
    assert "production_side_effect" in decision["reasons"], decision
    # The model's own opinion is carried for the record and read by nothing.
    assert decision["llm_confidence_used"] is False, decision
    assert decision["mode"] == "deterministic", decision

    gate_audit = _audit_for("it.risk_gate_decided", case["ticket_id"])
    assert len(gate_audit) == 1, gate_audit
    detail = gate_audit[0]["detail"]
    assert detail["rule_id"] == decision["rule_id"], detail
    assert detail["inputs"]["environment"] == "production", detail
    assert detail["inputs"]["evidence_count"] >= 1, detail
    assert detail["llm_confidence_used"] is False, detail

    # The approval names the IT action, not the create_ticket default.
    approval = _approval(case["approval_id"])
    assert approval["action_type"] == "service_restart", approval
    assert approval["tool_name"] == "restart_service", approval
    assert approval["run_id"] == case["workflow_run_id"], approval
    assert approval["status"] == "pending", approval
    assert approval["payload"]["ticket_id"] == case["ticket_id"], approval["payload"]
    assert "proposed_ticket" not in approval["payload"], "an IT approval must not create a second ticket"

    requested = _audit_for("it.approval_requested", case["ticket_id"])
    assert len(requested) == 1, requested
    assert requested[0]["detail"]["tool_name"] == "restart_service", requested[0]
    assert requested[0]["detail"]["risk_rule_id"] == decision["rule_id"], requested[0]

    workflow = get_run_detail(case["workflow_run_id"])
    node_names = [step["node_name"] for step in workflow["steps"]]
    assert "it_request_approval" in node_names, node_names
    assert "it_approval_ticket" in node_names, node_names
    assert "it_action_execute" not in node_names, node_names

    # Requirement: one incident, one ticket. The approval must not open a second.
    tickets = [ticket for ticket in _it_tickets() if REDIS_INCIDENT in (ticket["description"] or "")]
    assert len(tickets) == 1, [ticket["id"] for ticket in tickets]


# --------------------------------------------------------------------------- #
# 2. No knowledge -> NO_KNOWLEDGE -> a human, never an invented answer
# --------------------------------------------------------------------------- #


def _no_knowledge_evidence_goes_to_a_human() -> None:
    intake = submit_it_request(REDIS_SLOW, auth_context=EMPLOYEE)
    ticket_id = intake["ticket_id"]

    with get_connection() as conn:
        conn.execute("DELETE FROM knowledge_articles")
    try:
        response = it_resolve_request(ticket_id, ITResolveRequest(), auth_context=EMPLOYEE)
    finally:
        # Restored immediately: the seeded corpus is a fixture for every other
        # case, and leaving the table empty would make later failures confusing.
        seed_it_knowledge()
    assert HANDBOOK_TITLE in _knowledge_titles(), "the corpus must be back for the cases that follow"

    resolution = response["resolution"]
    assert resolution["status"] == "NO_KNOWLEDGE", resolution
    assert resolution["diagnosis"] is None, resolution
    assert resolution["proposed_action"] is None, resolution
    assert resolution["action_type"] == "no_action", resolution
    assert resolution["action_arguments"] == {}, resolution
    assert resolution["confidence"] == 0.0, resolution
    assert resolution["reason"] == "no_knowledge_evidence", resolution
    assert resolution["route"] == "human_handoff", resolution
    assert resolution["evidence"] == [], resolution
    assert resolution["mode"] == "deterministic_no_knowledge", resolution

    decision = response["risk_decision"]
    assert decision["decision"] == "require_approval", decision
    assert decision["executable"] is False, "an executable action is required before anything can run"
    assert decision["rule_id"] == "triage_requires_approval", decision
    # Two rules apply and both are recorded; the headline is whichever fired
    # first in rule order. The absence of evidence is the second one here
    # because a production incident already needed a human on triage's own
    # deterministic signal.
    assert decision["reasons"] == [
        "triage_requires_approval",
        "no_knowledge_handoff",
        "no_executable_action",
    ], decision["reasons"]
    assert decision["llm_confidence"] == 0.0, decision

    no_knowledge = _audit_for("it.resolution_no_knowledge", ticket_id)
    assert len(no_knowledge) == 1, no_knowledge
    assert no_knowledge[0]["detail"]["evidence_count"] == 0, no_knowledge[0]
    assert no_knowledge[0]["detail"]["reason"] == "no_knowledge_evidence", no_knowledge[0]
    assert _audit_for("it.resolution_proposed", ticket_id) == [], "nothing may be proposed without evidence"

    # Nothing ran, and nothing was refused on the ticket's behalf either: the
    # graph never reached the execution branch at all.
    node_names = [step["node_name"] for step in get_run_detail(response["workflow_run_id"])["steps"]]
    assert "it_action_execute" not in node_names, node_names
    assert "it_action_denied" not in node_names, node_names
    assert _audit_for("it.action_executed", ticket_id) == []
    assert _mcp_calls(*IT_ACTION_TOOLS) == 0, "no IT tool may be reached without evidence"

    # It goes to a human instead: the ticket parks, it is not resolved.
    assert response["status"] == "waiting_approval", response["status"]
    assert _ticket_status_timeline(ticket_id) == ["open", "investigating", "waiting_approval"]


# --------------------------------------------------------------------------- #
# 3. A reversible action outside production is automatic
# --------------------------------------------------------------------------- #


def _reversible_outside_production_runs_automatically() -> None:
    ticket_id = _it_action_ticket("it-resolution:dev-auto", asset_id="SERVER-001", environment="staging")

    response = it_resolve_request(ticket_id, ITResolveRequest(objective=CACHE_PRESSURE), auth_context=EMPLOYEE)

    resolution = response["resolution"]
    assert resolution["action_type"] == "cache_flush", resolution
    assert resolution["proposed_action"] == "flush_cache", resolution
    assert resolution["action_arguments"] == {"asset_id": "SERVER-001"}, resolution
    assert resolution["environment"] == "staging", resolution

    decision = response["risk_decision"]
    assert decision["decision"] == "auto_execute", decision
    assert decision["rule_id"] == "non_production_reversible_action", decision
    assert decision["executable"] is True, decision
    assert decision["environment"] == "staging", decision
    # The model suggested a confidence here and the gate still ran its own
    # rules; nothing about the outcome came from that number.
    assert decision["llm_confidence_used"] is False, decision

    execution = response["execution"]
    assert execution and execution["executed"] is True, execution
    assert execution["reason"] is None, execution
    assert execution["risk_class"] == "reversible_write", execution
    assert execution["result"]["simulated"] is True, execution

    executed = _audit_for("it.action_executed", ticket_id)
    assert len(executed) == 1, executed
    detail = executed[0]["detail"]
    assert detail["tool_name"] == "flush_cache", detail
    assert detail["asset_id"] == "SERVER-001", detail
    assert detail["approval_id"] is None, "an automatic action has no approval behind it"
    assert detail["executed_by"] == IT_SERVICE_ACCOUNT["user_id"], detail
    assert detail["requested_by"] == EMPLOYEE.user_id, detail
    assert detail["risk_rule_id"] == "non_production_reversible_action", detail
    assert detail["result_summary"]["executed"] is True, detail

    # No approval row exists for this run: the gate never asked for one.
    assert _approvals_for_run(response["workflow_run_id"]) == []

    assert response["status"] == "completed", response["status"]
    assert get_ticket(ticket_id)["status"] == "resolved", get_ticket(ticket_id)
    assert _ticket_status_timeline(ticket_id) == ["open", "investigating", "resolved"]
    node_names = [step["node_name"] for step in get_run_detail(response["workflow_run_id"])["steps"]]
    assert "it_action_execute" in node_names and "it_action_finalize" in node_names, node_names

    # The privileged call went through the registry, not around it.
    assert _mcp_calls("flush_cache") == 1, _mcp_calls("flush_cache")


# --------------------------------------------------------------------------- #
# 5. A permission grant always needs a human, in every environment
# --------------------------------------------------------------------------- #


def _permission_grant_requires_a_human() -> None:
    intake = submit_it_request(PERMISSION_REQUEST, auth_context=EMPLOYEE)
    assert intake["triage"]["intent"] == "PERMISSION_REQUEST", intake["triage"]
    assert intake["triage"]["entities"]["resource"] == "DATABASE", intake["triage"]
    ticket_id = intake["ticket_id"]

    response = it_resolve_request(ticket_id, ITResolveRequest(), auth_context=EMPLOYEE)

    resolution = response["resolution"]
    assert resolution["action_type"] == "permission_grant", resolution
    assert resolution["action_arguments"] == {
        "employee_id": EMPLOYEE.user_id,
        "resource": "DATABASE",
        "access_level": "read_only",
    }, resolution

    decision = response["risk_decision"]
    assert decision["decision"] == "require_approval", decision
    assert decision["rule_id"] == "action_class_requires_approval:permission_change", decision
    assert decision["executable"] is True, decision

    approval = response["approval"]
    assert approval["action_type"] == "permission_grant", approval
    assert approval["tool_name"] == "grant_permission", approval

    assert response["status"] == "waiting_approval", response["status"]
    assert _ticket_status_timeline(ticket_id) == ["open", "investigating", "waiting_approval"]
    assert _audit_for("it.action_executed", ticket_id) == []
    assert _mcp_calls("grant_permission") == 0, "grant_permission must not be called before the decision"


# --------------------------------------------------------------------------- #
# 6. Approve -> the workflow resumes on its own thread and the action runs
# --------------------------------------------------------------------------- #


def _approved_action_resumes_and_executes(case: dict) -> None:
    ticket_id = case["ticket_id"]
    before = _mcp_calls("restart_service")

    decided = decide_approval_and_resume(case["approval_id"], True, IT_ADMIN.user_id, "变更窗口已批准。")
    assert decided and decided["status"] == "completed", decided
    # The approval closed out the existing IT ticket rather than opening one.
    assert decided["ticket_id"] == ticket_id, decided
    assert get_ticket(ticket_id)["status"] == "approved", get_ticket(ticket_id)
    assert _mcp_calls("restart_service") == before, "approving is not executing"

    resumed = resume_multi_agent_for_workflow(decided)
    assert resumed, resumed
    assert resumed["id"] == case["run_id"], "the resume must reuse the original run and thread"
    assert resumed["status"] == "completed", resumed["status"]
    assert resumed["critic_report"]["passed"] is True, resumed["critic_report"]

    execution = _it_loop_state(resumed["id"])["it_execution"]
    assert execution["executed"] is True, execution
    assert execution["approval_id"] == case["approval_id"], execution
    assert execution["tool_name"] == "restart_service", execution

    executed = _audit_for("it.action_executed", ticket_id)
    assert len(executed) == 1, executed
    detail = executed[0]["detail"]
    assert detail["tool_name"] == "restart_service", detail
    assert detail["asset_id"] == "REDIS-001", detail
    assert detail["approval_id"] == case["approval_id"], detail
    assert detail["executed_by"] == IT_SERVICE_ACCOUNT["user_id"], detail
    assert detail["requested_by"] == EMPLOYEE.user_id, detail
    assert detail["risk_rule_id"] == "action_class_requires_approval:service_restart", detail
    assert detail["multi_agent_run_id"] == resumed["id"], detail
    assert detail["result_summary"]["text"], detail

    assert _mcp_calls("restart_service") == before + 1, _mcp_calls("restart_service")
    assert get_ticket(ticket_id)["status"] == "resolved", get_ticket(ticket_id)
    assert _ticket_status_timeline(ticket_id) == [
        "open",
        "investigating",
        "waiting_approval",
        "approved",
        "resolved",
    ]

    # Requirement 十: the audit trail answers "why did this execute?" from the
    # ticket alone, and it does so in causal order — the retrieval that was
    # chosen, the evidence it returned, the precedent consulted beside it, the
    # proposal built on the evidence, the rule that gated it, the approval that
    # unlocked it, and the execution itself.
    #
    # ``it.historical_retrieved`` (Phase 3) sits between the query and the
    # proposal because that is when it runs. It is asserted here rather than
    # tolerated as noise: an event that fires on every IT ticket is part of the
    # documented sequence, and a ticket's chain reading nine steps is the
    # guarantee that the historical channel was consulted — and, on a run where
    # it returned nothing, that it was consulted and came back empty.
    assert _it_event_sequence(ticket_id) == [
        # Written by Phase 1's intake when the reporter filed it.
        "it.request_submitted",
        "it.triage_classified",
        "it.research_query_built",
        "it.historical_retrieved",
        "it.resolution_proposed",
        "it.risk_gate_decided",
        "it.approval_requested",
        "it.ticket_linked",
        "it.action_executed",
    ], _it_event_sequence(ticket_id)

    # The gate's audit row carries the inputs its rule read, so the decision can
    # be re-derived later without trusting the row's own conclusion.
    gate = _audit_for("it.risk_gate_decided", ticket_id)[0]["detail"]
    assert gate["inputs"] == {
        "action_type": "service_restart",
        "actor_role": "employee",
        "criticality": "critical",
        "environment": "production",
        "evidence_count": 2,
        "missing_information": [],
        "triage_needs_approval": True,
    }, gate["inputs"]


# --------------------------------------------------------------------------- #
# 7. Reject -> nothing executes, and the decision cannot be replayed
# --------------------------------------------------------------------------- #


def _rejected_action_never_executes() -> None:
    intake = submit_it_request(REDIS_DOWN, auth_context=EMPLOYEE)
    ticket_id = intake["ticket_id"]
    response = it_resolve_request(ticket_id, ITResolveRequest(), auth_context=EMPLOYEE)
    approval_id = response["approval"]["id"]
    assert response["risk_decision"]["decision"] == "require_approval", response["risk_decision"]

    before = _mcp_calls(*IT_ACTION_TOOLS)

    decided = decide_approval_and_resume(approval_id, False, IT_ADMIN.user_id, "变更窗口不批准。")
    assert decided and decided["status"] == "cancelled", decided
    assert decided["ticket_id"] == ticket_id, "the denial must act on the IT ticket, not a new one"

    resumed = resume_multi_agent_for_workflow(decided)
    assert resumed and resumed["status"] == "cancelled", resumed
    assert resumed["critic_report"]["approval_denied_safely"] is True, resumed["critic_report"]

    skipped = _audit_for("it.action_not_executed", ticket_id)
    assert len(skipped) == 1, skipped
    assert skipped[0]["detail"]["reason"] == "approval_denied", skipped[0]
    assert skipped[0]["detail"]["tool_name"] == "restart_service", skipped[0]
    assert skipped[0]["detail"]["approval_id"] == approval_id, skipped[0]

    assert _mcp_calls(*IT_ACTION_TOOLS) == before, "a denied approval must not reach any tool"
    assert _audit_for("it.action_executed", ticket_id) == []
    assert get_ticket(ticket_id)["status"] == "rejected", get_ticket(ticket_id)

    # A decision already applied cannot be applied again — not flipped, and not
    # re-run. ``decision_applied`` is the guard that says so, and the ticket
    # count proves no second ticket appeared either.
    tickets_before = len(_it_tickets())
    replayed = decide_approval_and_resume(approval_id, True, IT_ADMIN.user_id, "试着重放。")
    assert replayed is not None, replayed
    assert replayed["status"] == "cancelled", replayed
    assert _mcp_calls(*IT_ACTION_TOOLS) == before, "a replayed decision must not execute anything"
    assert get_ticket(ticket_id)["status"] == "rejected", get_ticket(ticket_id)
    assert len(_it_tickets()) == tickets_before, "a replayed decision must not create a ticket"


# --------------------------------------------------------------------------- #
# 8. The risk gate itself cannot be bypassed
# --------------------------------------------------------------------------- #


def _risk_gate_rules() -> None:
    """Requirement 六 as a pure function: no database, no graph, no model."""
    # 6. Destructive and unrecognised actions are refused outright.
    destructive = evaluate_risk(action_type="data_delete")
    assert destructive.decision == "deny", destructive
    assert destructive.rule_id == "denied_action_class:destructive", destructive
    assert destructive.executable is False, destructive
    assert destructive.tool_name is None, destructive

    for action_type in ("permission_revoke", "account_disable"):
        denied = evaluate_risk(action_type=action_type)
        assert denied.decision == "deny", (action_type, denied)
        assert denied.rule_id == "denied_action_class:destructive", (action_type, denied)

    unknown = evaluate_risk(action_type="drop_table")
    assert unknown.decision == "deny", unknown
    assert unknown.rule_id == "unknown_action_type", unknown
    assert unknown.executable is False, unknown
    assert evaluate_risk(action_type=None).decision == "deny", "fail closed on a missing action type"

    # 1. A read-only check is automatic even in production.
    read_only = evaluate_risk(action_type="diagnostic_read", environment="production", evidence_count=2)
    assert read_only.decision == "auto_execute", read_only
    assert read_only.rule_id == "read_only_action", read_only
    assert read_only.executable is True, read_only

    # 2. A reversible action outside production is automatic.
    for environment in ("dev", "staging"):
        staging_flush = evaluate_risk(action_type="cache_flush", environment=environment, evidence_count=2)
        assert staging_flush.decision == "auto_execute", (environment, staging_flush)
        assert staging_flush.rule_id == "non_production_reversible_action", (environment, staging_flush)

    # 3. The same action in production is not.
    production_flush = evaluate_risk(action_type="cache_flush", environment="production", evidence_count=2)
    assert production_flush.decision == "require_approval", production_flush
    assert production_flush.rule_id == "production_side_effect", production_flush

    # 4. and 5.: permission grants and restarts need a human even in dev.
    grant = evaluate_risk(action_type="permission_grant", environment="dev", evidence_count=2)
    assert grant.decision == "require_approval", grant
    assert grant.rule_id == "action_class_requires_approval:permission_change", grant

    restart = evaluate_risk(action_type="service_restart", environment="dev", evidence_count=2)
    assert restart.decision == "require_approval", restart
    assert restart.rule_id == "action_class_requires_approval:service_restart", restart

    # A critical asset escalates even outside production.
    critical = evaluate_risk(
        action_type="cache_flush", environment="staging", criticality="critical", evidence_count=2
    )
    assert critical.decision == "require_approval", critical
    assert critical.rule_id == "critical_asset_side_effect", critical

    # No evidence: not executable, whatever else is true. Approval unlocks an
    # executable action; it does not make an un-runnable one runnable.
    blind = evaluate_risk(action_type="diagnostic_read", environment="production", evidence_count=0)
    assert blind.decision == "require_approval", blind
    assert blind.rule_id == "no_knowledge_handoff", blind
    assert blind.executable is False, blind

    # **The point of the whole file.** The model's confidence is echoed and never
    # consulted: 0.99 does not unlock a grant, 0.0 does not block a read-only
    # check, and 1.0 does not rescue a destructive one.
    confident = evaluate_risk(
        action_type="permission_grant", environment="dev", evidence_count=3, llm_confidence=0.99
    )
    assert confident.decision == "require_approval", confident
    assert confident.llm_confidence == 0.99, confident
    assert confident.llm_confidence_used is False, confident

    unconfident = evaluate_risk(
        action_type="diagnostic_read", environment="production", evidence_count=3, llm_confidence=0.0
    )
    assert unconfident.decision == "auto_execute", unconfident
    assert unconfident.llm_confidence_used is False, unconfident

    certain_but_denied = evaluate_risk(action_type="data_delete", llm_confidence=1.0)
    assert certain_but_denied.decision == "deny", certain_but_denied

    # Every decision says it is deterministic, so the audit trail cannot be
    # mistaken for a model's judgement call.
    modes = {
        evaluate_risk(action_type=name).mode
        for name in ("diagnostic_read", "data_delete", "permission_grant")
    }
    assert modes == {"deterministic"}, modes


def _denied_action_never_reaches_a_tool() -> None:
    """Even a resolution that asks for a destructive action is refused downstream."""
    ticket_id = _it_action_ticket("it-resolution:denied-action", asset_id="REDIS-001", environment="production")
    before = _mcp_calls(*IT_ACTION_TOOLS)

    original_run = ResolutionAgent.run

    # ``**extra`` rather than an explicit ``historical_output``: the wrapper's
    # job is to be indistinguishable from the real thing, so it must forward
    # whatever the caller passes rather than track the signature by hand. A
    # hand-written list here would have silently turned "the agent grew a
    # parameter" into "the resolution is None".
    def _escalated_resolution(self, objective, **extra):
        resolution = original_run(self, objective, **extra)
        # A resolution agent that has been talked into proposing something the
        # platform refuses. ``action_type`` is the only field the gate reads.
        if resolution.get("action_type") != "no_action":
            resolution["action_type"] = "data_delete"
            resolution["proposed_action"] = "delete_volume"
            resolution["confidence"] = 0.99
        return resolution

    ResolutionAgent.run = _escalated_resolution
    try:
        response = it_resolve_request(
            ticket_id, ITResolveRequest(objective=CACHE_PRESSURE), auth_context=EMPLOYEE
        )
    finally:
        ResolutionAgent.run = original_run

    assert response["resolution"]["action_type"] == "data_delete", response["resolution"]

    decision = response["risk_decision"]
    assert decision["decision"] == "deny", decision
    assert decision["rule_id"] == "denied_action_class:destructive", decision
    assert decision["executable"] is False, decision
    assert decision["llm_confidence"] == 0.99, decision
    assert decision["llm_confidence_used"] is False, decision

    assert response["execution"]["executed"] is False, response["execution"]
    assert response["execution"]["reason"] == "risk_deny", response["execution"]

    denied = _audit_for("it.action_denied", ticket_id)
    assert len(denied) == 1, denied
    assert denied[0]["detail"]["layer"] == "risk_gate", denied[0]
    assert denied[0]["detail"]["rule_id"] == "denied_action_class:destructive", denied[0]
    assert denied[0]["detail"]["action_type"] == "data_delete", denied[0]

    # No approval was opened for it, no tool was called, and nothing was changed.
    assert _approvals_for_run(response["workflow_run_id"]) == []
    assert _mcp_calls(*IT_ACTION_TOOLS) == before, "a denied action must never reach a tool"
    assert _audit_for("it.action_executed", ticket_id) == []

    assert response["status"] == "cancelled", response["status"]
    assert get_ticket(ticket_id)["status"] == "rejected", get_ticket(ticket_id)


# --------------------------------------------------------------------------- #
# 10. A tool that fails is recorded, and is not retried into a second outage
# --------------------------------------------------------------------------- #


def _failed_tool_call_is_audited() -> None:
    result = call_tool("restart_service", {"asset_id": "ASSET-NOPE"}, auth_context=IT_ADMIN)
    assert result.get("error"), result
    assert "asset_not_found" in str(result["error"]), result

    errors = [entry for entry in _audit("mcp.tool_error") if entry["target_id"] == "restart_service"]
    assert errors, "a raising tool must be recorded as mcp.tool_error"
    assert errors[-1]["detail"]["arguments"]["asset_id"] == "ASSET-NOPE", errors[-1]
    assert "asset_not_found" in errors[-1]["detail"]["error"], errors[-1]

    failures = [entry for entry in _audit("it.action_failed") if entry["target_id"] == "ASSET-NOPE"]
    assert failures, "the tool's own precondition failure must be recorded as it.action_failed"
    assert failures[-1]["detail"]["tool_name"] == "restart_service", failures[-1]
    assert failures[-1]["detail"]["action_type"] == "service_restart", failures[-1]
    assert failures[-1]["detail"]["executed_by"] == IT_ADMIN.user_id, failures[-1]
    assert failures[-1]["detail"]["error_type"] == "ITOperationError", failures[-1]


def _failed_action_fails_the_run_without_a_retry() -> None:
    ticket_id = _it_action_ticket(
        "it-resolution:tool-failure", asset_id="ASSET-NOPE", environment="staging"
    )
    before = _mcp_calls(*IT_ACTION_TOOLS)

    response = it_resolve_request(ticket_id, ITResolveRequest(objective=CACHE_PRESSURE), auth_context=EMPLOYEE)

    # Phase 4: an asset the platform cannot find is not an asset it may treat as
    # non-production. The ticket says staging, the directory has never heard of
    # ASSET-NOPE, so the gate cannot verify the target and takes the worst case.
    # This is the fail-closed half of the fix; the run below still proves the
    # original point, that a broken tool is recorded and never retried.
    assert response["risk_decision"]["decision"] == "require_approval", response["risk_decision"]
    assert response["risk_decision"]["rule_id"] == "production_side_effect", response["risk_decision"]
    assert response["risk_decision"]["inputs"]["environment"] == "production", response["risk_decision"]
    assert _mcp_calls(*IT_ACTION_TOOLS) == before, "an unverifiable target must not be acted on"

    decided = decide_approval_and_resume(
        response["approval"]["id"], True, IT_ADMIN.user_id, "资产信息缺失，人工确认。"
    )
    assert decided and decided["status"] == "completed", decided
    resumed = resume_multi_agent_for_workflow(decided)
    assert resumed, resumed

    execution = _it_loop_state(resumed["id"])["it_execution"]
    assert execution and execution["executed"] is False, execution
    assert execution["reason"] == "tool_error", execution

    # A broken tool is a failure, not a refusal: the two end differently so an
    # operator can tell them apart. Going through approval changes what the run
    # reports — the workflow itself completed, because the human decided — so
    # the distinction now shows up where it matters, on the ticket: "approved
    # for human handling, not for execution" hands it back to a person rather
    # than closing it as rejected.
    assert resumed["status"] == "completed", resumed["status"]
    ticket = get_ticket(ticket_id)
    assert ticket["status"] == "investigating", ticket
    with get_connection() as conn:
        bodies = [
            row["body"]
            for row in rows_to_dicts(
                conn.execute(
                    "SELECT body FROM ticket_events WHERE ticket_id = ? ORDER BY rowid ASC",
                    (ticket_id,),
                ).fetchall()
            )
        ]
    assert any("not for execution" in body for body in bodies), bodies

    errors = [
        entry
        for entry in _audit("mcp.tool_error")
        if (entry["detail"].get("arguments") or {}).get("asset_id") == "ASSET-NOPE"
    ]
    assert errors, "the failed tool call must be in the audit chain"

    # Re-running a restart is not a retry, it is a second outage window. The
    # action step is ``it_operation``, and the policy that governs it is what
    # makes the attempt count 1 no matter how the tool failed.
    #
    # Asserted against the policy rather than against a workflow step because
    # this run reached the tool through approval, and the approved branch
    # executes the action directly instead of materialising an
    # ``it_action_execute`` step — see ``_execute_it_action_after_approval``.
    # The automatic branch still records that step; section 3 above checks its
    # node names.
    policy = default_retry_policy("flush_cache", "it_operation")
    assert policy.retryable is False, policy
    assert policy.max_attempts == 1, policy


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _it_action_ticket(agent_run_id: str, *, asset_id: str, environment: str) -> str:
    """A ticket whose triage is fixed by the fixture rather than re-derived.

    ``agent_run_id`` is what keeps two otherwise identical fixtures distinct:
    the idempotency key includes it, so a second fixture with the same words
    would return the first ticket instead of creating a new one.
    """
    triage = {
        "intent": "IT_INCIDENT",
        "category": "REDIS",
        "priority": "normal",
        "entities": {"service": "REDIS", "environment": environment},
        "needs_approval": False,
        "missing_information": [],
        "confidence": 0.85,
        "mode": "deterministic",
    }
    ticket = call_tool(
        "create_ticket",
        {
            "title": CACHE_PRESSURE,
            "description": "\n".join(
                ["IT service intake (test fixture).", "", "Original request:", CACHE_PRESSURE]
            ),
            "priority": "normal",
            "owner_department": "IT",
            "workflow_type": "it_service_intake",
            "category": "REDIS",
            "risk_level": "low",
            "agent_run_id": agent_run_id,
            "tenant_id": "default",
            "requester_user_id": EMPLOYEE.user_id,
            "it_category": "REDIS",
            "service": "REDIS",
            "asset_id": asset_id,
            "environment": environment,
            "triage": triage,
        },
        actor=EMPLOYEE.user_id,
        source="it_resolution_smoke_test",
        auth_context=EMPLOYEE,
    )
    assert ticket.get("id"), ticket
    assert ticket["status"] == "open", ticket
    return ticket["id"]


def _ticket_status_timeline(ticket_id: str) -> list[str]:
    """Status transitions in causal order. Comment events carry no status and drop out.

    Read by ``rowid`` rather than through ``list_ticket_events``. That API orders
    ``created_at DESC, id DESC``, and event ids are random hex rather than
    monotonic — so events written inside the same second come back in arbitrary
    order. The transitions this file asserts on all happen within the same
    second, which is exactly the case where that ordering carries no
    information. ``rowid`` is insertion order, which is the order the code
    wrote them in.

    ``list_ticket_events`` is still exercised once in
    ``_redis_incident_reaches_risk_gate`` so this test also covers the API the
    demo's ``GET /api/it/requests/{id}`` timeline uses.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT to_status FROM ticket_events WHERE ticket_id = ? ORDER BY rowid ASC",
            (ticket_id,),
        ).fetchall()
    return [row["to_status"] for row in rows_to_dicts(rows) if row["to_status"]]


def _approval(approval_id: str) -> dict:
    return next(item for item in list_approvals(limit=500) if item["id"] == approval_id)


def _approvals_for_run(workflow_run_id: str) -> list[dict]:
    return [item for item in list_approvals(limit=500) if item.get("run_id") == workflow_run_id]


def _audit(event_type: str) -> list[dict]:
    """Every audit row of one event type, oldest first.

    ``list_audit_logs`` returns the newest page, capped at 500 rows; this script
    writes past that and asserts on the whole history, so the filter runs in the
    query rather than over a truncated page.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM audit_logs
            WHERE event_type = ?
            ORDER BY created_at ASC, id ASC
            """,
            (event_type,),
        ).fetchall()
    return [hydrate_audit_log(item) for item in rows_to_dicts(rows)]


def _audit_for(event_type: str, ticket_id: str) -> list[dict]:
    return [entry for entry in _audit(event_type) if entry["target_id"] == ticket_id]


def _it_event_sequence(ticket_id: str) -> list[str]:
    """The IT events recorded against one ticket, in the order they happened.

    Ordered by ``rowid`` (insertion order). The ``it.*`` trail is written by the
    graph as it walks, so this reads as the reasoning behind the action; the
    ``ticket.*`` rows interleaved with it are the generic ticket lifecycle and
    are left out to keep the sequence about the IT loop.
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT event_type FROM audit_logs
            WHERE target_type = 'ticket' AND target_id = ? AND event_type LIKE 'it.%'
            ORDER BY rowid ASC
            """,
            (ticket_id,),
        ).fetchall()
    return [row["event_type"] for row in rows_to_dicts(rows)]


def _mcp_calls(*tool_names: str) -> int:
    """How many times these tools were actually invoked, not merely attempted."""
    return len([entry for entry in _audit("mcp.tool_call") if entry["target_id"] in tool_names])


def _it_tickets() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM tickets WHERE owner_department = 'IT'").fetchall()
    return rows_to_dicts(rows)


def _knowledge_titles() -> list[str]:
    with get_connection() as conn:
        rows = conn.execute("SELECT title FROM knowledge_articles").fetchall()
    return [row["title"] for row in rows_to_dicts(rows)]


if __name__ == "__main__":
    main()
