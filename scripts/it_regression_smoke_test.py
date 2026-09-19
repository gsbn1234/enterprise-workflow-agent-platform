"""Phase 4 regression suite: the three defects the Phase 3 evaluation surfaced.

Phase 3 shipped an evaluation harness that was *meant* to stay red on three
cases, and it did. This file is the other half of that work: one case per fix,
written so that reverting any of the three fixes turns a green case red.

    1  production asset + cache_flush     -> a human, and nothing runs before them
    2  production asset + service_restart -> a human, and a rejection runs nothing
    3  critical asset + mutating action   -> never automatic, even with no
                                             environment word in the request
    4  a denied action class              -> zero tool calls, ticket rejected
    5  "Redis + Database" in one sentence -> the answer does not follow word order
    6  "申请安装 Docker，需要管理员权限"    -> a software request, not a permission one
    7  "申请生产数据库访问权限"            -> a permission request, not a software one

Cases 1-4 drive the real intake and resolve routes; 5-7 exercise the
deterministic classifier directly, because "which category does this sentence
land in" is a property of ``classify`` and needs no graph around it. Case 6 and
7 also drive the route, so the category is not merely asserted in isolation but
shown to choose the action that follows from it.

Nothing here contacts a real system: mock providers, no RAG service, seeded
deterministic corpus, LLM off. The risk gate is pure code, and a run that passes
only when a model agrees with it would prove nothing about the gate.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "it_regression_smoke_test.sqlite3")
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["AGENT_LLM_ENABLED"] = "false"
sys.path.insert(0, str(ROOT))

from app.db import get_connection, reset_database, rows_to_dicts  # noqa: E402
from app.main import _it_loop_state, it_resolve_request  # noqa: E402
from app.schemas import ITResolveRequest  # noqa: E402
from app.services.agent import decide_approval_and_resume  # noqa: E402
from app.services.audit import hydrate_audit_log  # noqa: E402
from app.services.auth import AuthContext, ensure_demo_users  # noqa: E402
from app.services.it.intake import submit_it_request  # noqa: E402
from app.services.it.risk_gate import evaluate as evaluate_risk  # noqa: E402
from app.services.it.triage import classify  # noqa: E402
from app.services.multi_agent import resume_multi_agent_for_workflow  # noqa: E402
from app.services.multi_agent.agents import ResolutionAgent  # noqa: E402
from app.services.tenancy import set_current_tenant_id  # noqa: E402
from app.services.tools.approvals import list_approvals  # noqa: E402
from app.services.tools.ticketing import get_ticket  # noqa: E402


EMPLOYEE = AuthContext(
    user_id="E002", display_name="李四", department="Engineering", role="employee", tenant_id="default"
)
IT_ADMIN = AuthContext(
    user_id="E003", display_name="王五", department="IT", role="it_admin", tenant_id="default"
)

# One sentence per case, and none of them names an asset id. The whole point of
# the P0 fix is that the platform finds the asset itself, from the service the
# reporter names, and then believes the asset table over the prose.
#
# "内网" rather than "预发" in case 3 is deliberate: it carries no environment
# signal at all, so anything the gate concludes about the environment can only
# have come from the asset row.
CACHE_FLUSH_TEXT = "预发环境的 Redis 缓存需要清理"
RESTART_TEXT = "预发环境的 Redis 需要重启一下"
NO_ENV_TEXT = "内网的 Redis 缓存压力很大，需要清理缓存"
DENIED_TEXT = "内网的 Redis 服务异常，需要重启一下"
DOCKER_TEXT = "申请安装 Docker，需要管理员权限"
DB_PERMISSION_TEXT = "申请生产数据库访问权限"

# Every registered IT action tool, so a case can assert that *none* of them ran
# without listing the four by hand in four different places. ``get_asset`` is
# not in the list on purpose: it is a read the risk gate performs to learn what
# it is deciding about, not a mutating action, and counting it here would make
# "no tool ran" false for a reason that has nothing to do with safety.
IT_ACTION_TOOLS = ("diagnose_service", "flush_cache", "restart_service", "grant_permission")


def main() -> None:
    reset_database(seed=True)
    ensure_demo_users()
    # The routes normally get this from their auth dependency; this script calls
    # the route functions directly, so it sets the scope itself.
    set_current_tenant_id("default")

    # 1. The headline finding: production asset, reversible action, no approval.
    _production_asset_escalates_a_reversible_action()
    # 2. The same asset with an action class that always needs a human, and a
    #    rejection that must not execute anything.
    _rejected_approval_runs_nothing()
    # 3. §十一's worst case: no environment word anywhere, critical asset.
    _critical_asset_is_never_automatic()
    # 4. A denied class reaches no tool at all.
    _denied_class_reaches_no_tool()
    # 5. The classifier must not decide by keyword order.
    _service_classification_ignores_word_order()
    # 6 + 7. The two halves of the 授权 conflict.
    _permission_word_does_not_win_a_purchase()
    _access_request_is_still_a_permission_request()

    print("it_regression_smoke_test passed")
    print(f"it_tickets={len(_it_tickets())}")
    print(f"it_regressions=7")


# --------------------------------------------------------------------------- #
# Case 1 — production asset + cache_flush
# --------------------------------------------------------------------------- #


def _production_asset_escalates_a_reversible_action() -> None:
    """§三's example, end to end: the request never says production.

    "Redis 连不上，需要清理缓存" with a staging label is exactly the sentence the
    Phase 3 suite recorded as auto-executing. The target is REDIS-001, which the
    asset table records as production and critical, so the gate must read the
    asset rather than the prose.
    """
    intake = submit_it_request(CACHE_FLUSH_TEXT, auth_context=EMPLOYEE)
    ticket_id = intake["ticket_id"]
    assert intake["triage"]["entities"]["environment"] == "staging", intake["triage"]
    assert intake["related_asset"]["id"] == "REDIS-001", intake["related_asset"]
    assert intake["related_asset"]["environment"] == "production", intake["related_asset"]
    assert intake["related_asset"]["criticality"] == "critical", intake["related_asset"]

    before = _mcp_calls(*IT_ACTION_TOOLS)
    assert before == 0, "nothing may have run before the first resolve"

    response = it_resolve_request(ticket_id, ITResolveRequest(), auth_context=EMPLOYEE)

    resolution = response["resolution"]
    assert resolution["action_type"] == "cache_flush", resolution
    assert resolution["action_arguments"] == {"asset_id": "REDIS-001"}, resolution
    # The prose still says staging — that is what makes the asset read the thing
    # under test rather than a restatement of the text.
    assert resolution["environment"] == "staging", resolution

    decision = response["risk_decision"]
    assert decision["decision"] == "require_approval", decision
    assert decision["rule_id"] == "production_side_effect", decision
    assert decision["environment"] == "production", decision
    assert decision["inputs"]["environment"] == "production", decision
    assert decision["inputs"]["criticality"] == "critical", decision
    assert decision["reasons"] == ["production_side_effect", "critical_asset_side_effect"], decision

    # §四: a decision that requires approval may not reach a tool.
    assert _mcp_calls(*IT_ACTION_TOOLS) == 0, "an unapproved action must not reach a tool"
    assert _audit_for("it.action_executed", ticket_id) == []
    assert response["status"] == "waiting_approval", response["status"]
    assert get_ticket(ticket_id)["status"] == "waiting_approval", get_ticket(ticket_id)

    # §十二: "why did the gate ask for approval?" answered from one audit row.
    rows = _audit_for("it.risk_gate_decided", ticket_id)
    assert len(rows) == 1, rows
    detail = rows[0]["detail"]
    assert detail["asset_id"] == "REDIS-001", detail
    assert detail["asset_lookup"] == "found", detail
    assert detail["environment"] == "production", detail
    assert detail["criticality"] == "critical", detail
    assert detail["decision"] == "require_approval", detail
    assert detail["rule_id"] == "production_side_effect", detail
    assert detail["action_class"]["action_type"] == "cache_flush", detail["action_class"]
    assert detail["action_class"]["tool_name"] == "flush_cache", detail["action_class"]
    assert detail["action_class"]["side_effect"] is True, detail["action_class"]
    # Both sources are recorded, so a reader can see which one escalated.
    assert detail["asset_environment"] == "production", detail
    assert detail["asset_criticality"] == "critical", detail
    assert detail["text_environment"] == "staging", detail
    assert detail["text_criticality"] is None, detail

    # The approval exists because a human has to decide, and only then does the
    # action run. Approval is what unlocks it, not the agent's confidence.
    approval = response["approval"]
    assert approval and approval["status"] == "pending", approval
    assert approval["tool_name"] == "flush_cache", approval

    decided = decide_approval_and_resume(approval["id"], True, IT_ADMIN.user_id, "缓存清理已批准。")
    assert decided and decided["status"] == "completed", decided
    assert _mcp_calls(*IT_ACTION_TOOLS) == 0, "approving is not executing"

    resumed = resume_multi_agent_for_workflow(decided)
    assert resumed, resumed
    execution = _it_loop_state(resumed["id"])["it_execution"]
    assert execution["executed"] is True, execution
    assert execution["tool_name"] == "flush_cache", execution
    assert _mcp_calls("flush_cache") == 1, _mcp_calls("flush_cache")
    assert get_ticket(ticket_id)["status"] == "resolved", get_ticket(ticket_id)


# --------------------------------------------------------------------------- #
# Case 2 — production asset + service_restart, and a rejection
# --------------------------------------------------------------------------- #


def _rejected_approval_runs_nothing() -> None:
    """§八 case 2 and §十一's REJECT clause in one ticket.

    A restart always needs a human, so the headline rule is the action class
    rather than the asset — but the asset still escalates, and the run still
    stops. Rejecting it must leave the tool count where it was.
    """
    intake = submit_it_request(RESTART_TEXT, auth_context=EMPLOYEE)
    ticket_id = intake["ticket_id"]
    assert intake["related_asset"]["id"] == "REDIS-001", intake["related_asset"]
    # Case 1 already executed its approved flush, so this counts deltas rather
    # than expecting a globally empty history.
    before = _mcp_calls(*IT_ACTION_TOOLS)

    response = it_resolve_request(ticket_id, ITResolveRequest(), auth_context=EMPLOYEE)
    assert response["resolution"]["action_type"] == "service_restart", response["resolution"]

    decision = response["risk_decision"]
    assert decision["decision"] == "require_approval", decision
    # R2 fires before R3, so the class is still the headline for a restart. The
    # asset-derived reasons are recorded behind it.
    assert decision["rule_id"] == "action_class_requires_approval:service_restart", decision
    assert decision["reasons"] == [
        "action_class_requires_approval:service_restart",
        "production_side_effect",
        "critical_asset_side_effect",
    ], decision["reasons"]

    assert _mcp_calls(*IT_ACTION_TOOLS) == before, "nothing may run before the approval"

    approval = response["approval"]
    decided = decide_approval_and_resume(approval["id"], False, IT_ADMIN.user_id, "变更窗口不批准。")
    assert decided and decided["status"] == "cancelled", decided

    resumed = resume_multi_agent_for_workflow(decided)
    assert resumed and resumed["status"] == "cancelled", resumed

    # §四/§十一: REJECT -> 0 tool calls.
    assert _mcp_calls(*IT_ACTION_TOOLS) == before, "a rejected action must not reach a tool"
    assert _audit_for("it.action_executed", ticket_id) == []
    skipped = _audit_for("it.action_not_executed", ticket_id)
    assert len(skipped) == 1, skipped
    assert skipped[0]["detail"]["reason"] == "approval_denied", skipped[0]
    assert get_ticket(ticket_id)["status"] == "rejected", get_ticket(ticket_id)


# --------------------------------------------------------------------------- #
# Case 3 — a critical asset is never automatic
# --------------------------------------------------------------------------- #


def _critical_asset_is_never_automatic() -> None:
    """§十一's headline: no environment word in the request at all.

    Nothing in "内网的 Redis 缓存压力很大" says staging or production, so before
    the fix there was no environment to escalate on and the critical asset was
    never consulted either. The gate now reads both from the asset row.
    """
    intake = submit_it_request(NO_ENV_TEXT, auth_context=EMPLOYEE)
    ticket_id = intake["ticket_id"]
    assert "environment" not in (intake["triage"]["entities"] or {}), intake["triage"]
    assert intake["related_asset"]["id"] == "REDIS-001", intake["related_asset"]
    before = _mcp_calls(*IT_ACTION_TOOLS)

    response = it_resolve_request(ticket_id, ITResolveRequest(), auth_context=EMPLOYEE)
    assert response["resolution"]["environment"] is None, response["resolution"]

    decision = response["risk_decision"]
    assert decision["decision"] != "auto_execute", decision
    assert decision["decision"] == "require_approval", decision
    assert decision["environment"] == "production", decision
    assert decision["inputs"]["criticality"] == "critical", decision
    assert decision["rule_id"] == "production_side_effect", decision

    # ``executable`` is False here, and the reason is worth recording rather
    # than tuning away: with no environment word in the sentence, the resolution
    # agent reported ``missing_information: ["environment"]``, so R6 also fires
    # and marks the action un-runnable. The gate has since read the environment
    # off the asset, so the two stages now disagree — the gate is smarter than
    # the resolution that fed it. Both readings are safe (the ticket goes to a
    # human either way, and approval alone cannot unlock an un-runnable action),
    # so this stays as-is and is listed as a known limitation in the Phase 4
    # report rather than being papered over here.
    assert decision["executable"] is False, decision
    assert decision["reasons"] == [
        "production_side_effect",
        "critical_asset_side_effect",
        "request_more_information",
    ], decision["reasons"]
    assert response["resolution"]["missing_information"] == ["environment"], response["resolution"]

    assert _mcp_calls(*IT_ACTION_TOOLS) == before, "a critical asset must not be acted on unattended"
    assert response["status"] == "waiting_approval", response["status"]

    detail = _audit_for("it.risk_gate_decided", ticket_id)[0]["detail"]
    assert detail["asset_environment"] == "production", detail
    assert detail["asset_criticality"] == "critical", detail
    # The request supplied neither, which is the point: both came from the asset.
    assert detail["text_environment"] is None, detail
    assert detail["text_criticality"] is None, detail

    # §八 case 3, second half: R3b on its own, with R3 held quiet so the two
    # rules cannot mask each other. A staging environment is not enough to make
    # a critical asset automatic.
    critical_only = evaluate_risk(
        action_type="cache_flush", environment="staging", criticality="critical", evidence_count=2
    )
    assert critical_only.decision == "require_approval", critical_only
    assert critical_only.rule_id == "critical_asset_side_effect", critical_only
    assert critical_only.reasons == ("critical_asset_side_effect",), critical_only

    # and the merge itself only ever tightens: adding a source cannot lower it.
    assert (
        evaluate_risk(
            action_type="cache_flush", environment="dev", criticality="normal", evidence_count=2
        ).decision
        == "auto_execute"
    ), "the merge must not escalate a genuinely low-risk target"


# --------------------------------------------------------------------------- #
# Case 4 — a denied action class
# --------------------------------------------------------------------------- #


def _denied_class_reaches_no_tool() -> None:
    """§八 case 4 and §十一's DENY clause.

    ``data_delete`` is refused by rule R1 before any environment reasoning
    happens, so this is the one case where the asset metadata is irrelevant —
    which is exactly why it is worth asserting separately.
    """
    intake = submit_it_request(DENIED_TEXT, auth_context=EMPLOYEE)
    ticket_id = intake["ticket_id"]
    before = _mcp_calls(*IT_ACTION_TOOLS)

    original_run = ResolutionAgent.run

    # ``**extra`` rather than an explicit parameter list: the wrapper must be
    # indistinguishable from the real agent, so it forwards whatever the caller
    # passes instead of tracking the signature by hand.
    def _escalated_resolution(self, objective, **extra):
        resolution = original_run(self, objective, **extra)
        if resolution.get("action_type") != "no_action":
            resolution["action_type"] = "data_delete"
            resolution["proposed_action"] = "delete_volume"
            resolution["confidence"] = 0.99
        return resolution

    ResolutionAgent.run = _escalated_resolution
    try:
        response = it_resolve_request(ticket_id, ITResolveRequest(), auth_context=EMPLOYEE)
    finally:
        ResolutionAgent.run = original_run

    assert response["resolution"]["action_type"] == "data_delete", response["resolution"]

    decision = response["risk_decision"]
    assert decision["decision"] == "deny", decision
    assert decision["rule_id"] == "denied_action_class:destructive", decision
    assert decision["executable"] is False, decision
    # A model that is certain is still refused, and the row says the number was
    # never read.
    assert decision["llm_confidence"] == 0.99, decision
    assert decision["llm_confidence_used"] is False, decision

    # §十一: DENY -> Tool Calls = 0.
    assert _mcp_calls(*IT_ACTION_TOOLS) == before, "a denied action must never reach a tool"
    assert _audit_for("it.action_executed", ticket_id) == []
    assert _approvals_for_run(response["workflow_run_id"]) == [], "a denial asks no human"
    assert response["execution"]["reason"] == "risk_deny", response["execution"]
    assert get_ticket(ticket_id)["status"] == "rejected", get_ticket(ticket_id)

    denial = _audit_for("it.action_denied", ticket_id)
    assert len(denial) == 1, denial
    assert denial[0]["detail"]["layer"] == "risk_gate", denial[0]
    assert denial[0]["detail"]["rule_id"] == "denied_action_class:destructive", denial[0]


# --------------------------------------------------------------------------- #
# Case 5 — the classifier must not decide by keyword order
# --------------------------------------------------------------------------- #


def _service_classification_ignores_word_order() -> None:
    """§五: not fixed by moving a keyword, and this is the proof.

    ``SERVICE_KEYWORDS`` used to be read first-hit-wins, with REDIS before
    DATABASE, so the reporter's guess at a cause outranked the service they
    actually said was broken. The fix scores every hit by the clause it sits in.
    Writing the same sentence backwards is the test that separates "the table
    was reordered" from "the evidence is weighed": a reorder would flip with it.
    """
    symptom_first = classify("生产环境数据库连不上，怀疑是 Redis 缓存雪崩")
    assert symptom_first.intent == "IT_INCIDENT", symptom_first.to_dict()
    assert symptom_first.category == "DATABASE", symptom_first.to_dict()
    assert symptom_first.entities["service"] == "DATABASE", symptom_first.to_dict()

    # Same two facts, opposite order. A positional fix would answer REDIS here.
    guess_first = classify("怀疑是 Redis 缓存雪崩，生产环境数据库连不上")
    assert guess_first.category == "DATABASE", guess_first.to_dict()
    assert guess_first.entities["service"] == "DATABASE", guess_first.to_dict()

    # The same property with a different symptom word and a different guess, so
    # the fix is not tuned to one sentence: the observation still wins and the
    # hedge still loses, whichever way round they are written.
    for text in ("数据库连不上，可能是 Redis 挂了", "可能是 Redis 挂了，数据库连不上"):
        assert classify(text).entities["service"] == "DATABASE", (text, classify(text).to_dict())

    # The fix must not have flattened the table: a sentence that really is about
    # Redis still is, including when Redis and 服务器 both appear.
    for text in (
        "预发环境的服务器缓存压力很大，需要清理缓存",
        "我的生产 Redis 连不上了",
        "生产 Redis 缓存压力很大",
        "生产环境 Redis 异常",
    ):
        assert classify(text).entities["service"] == "REDIS", (text, classify(text).to_dict())

    # and the other services are untouched by the change.
    others = {
        "内网 DNS 解析失败": "NETWORK",
        "VPN 连不上": "VPN",
        "生产服务器挂了": "SERVER",
        "生产数据库锁表": "DATABASE",
        "邮箱登录不了": "EMAIL",
    }
    for text, expected in others.items():
        assert classify(text).entities["service"] == expected, (text, classify(text).to_dict())

    # A sentence about none of them stays unclassified rather than being pushed
    # onto whatever the scorer liked best.
    unrelated = classify("今天食堂几点开门？")
    assert unrelated.category == "GENERAL", unrelated.to_dict()
    assert "service" not in unrelated.entities, unrelated.to_dict()


# --------------------------------------------------------------------------- #
# Case 6 — "申请安装 Docker，需要管理员权限"
# --------------------------------------------------------------------------- #


def _permission_word_does_not_win_a_purchase() -> None:
    """§六 rule 1: the main intent is a purchase, so 权限 does not decide it.

    The request names a thing to install and a permission needed *to* install
    it. The permission word describes the action, not the object, and the object
    is what is being asked for.
    """
    triage = classify(DOCKER_TEXT)
    assert triage.intent == "SOFTWARE_REQUEST", triage.to_dict()
    assert triage.category == "DOCKER", triage.to_dict()
    assert triage.entities["software"] == "DOCKER", triage.to_dict()

    # and the classification picks the action that follows from it: software is
    # routed to a team, never installed by the agent.
    intake = submit_it_request(DOCKER_TEXT, auth_context=EMPLOYEE)
    before = _mcp_calls(*IT_ACTION_TOOLS)
    response = it_resolve_request(intake["ticket_id"], ITResolveRequest(), auth_context=EMPLOYEE)
    assert response["resolution"]["action_type"] == "no_action", response["resolution"]
    assert response["risk_decision"]["executable"] is False, response["risk_decision"]
    assert _mcp_calls(*IT_ACTION_TOOLS) == before, "the agent must not install software itself"

    # §六 rule 3, the other side of the same conflict: a licence is not access.
    paid = classify("申请采购付费数据库客户端授权 Navicat")
    assert paid.intent == "SOFTWARE_REQUEST", paid.to_dict()
    assert paid.category == "DB_CLIENT", paid.to_dict()

    # §六 rule 1 again, with the purchase marker instead of the install verb.
    purchase = classify("我要申请购买 Office 商业授权")
    assert purchase.intent == "SOFTWARE_REQUEST", purchase.to_dict()


# --------------------------------------------------------------------------- #
# Case 7 — "申请生产数据库访问权限"
# --------------------------------------------------------------------------- #


def _access_request_is_still_a_permission_request() -> None:
    """§六 rule 2: an explicit access request stays a permission request.

    The fix must not have over-corrected. This sentence names a resource and an
    access level and asks for neither a purchase nor an installation, so it is
    the same class of request it always was — and it still proposes the grant
    that class implies.
    """
    triage = classify(DB_PERMISSION_TEXT)
    assert triage.intent == "PERMISSION_REQUEST", triage.to_dict()
    assert triage.category == "DATABASE_PERMISSION", triage.to_dict()
    assert triage.entities["resource"] == "DATABASE", triage.to_dict()

    intake = submit_it_request(DB_PERMISSION_TEXT, auth_context=EMPLOYEE)
    before = _mcp_calls(*IT_ACTION_TOOLS)
    response = it_resolve_request(intake["ticket_id"], ITResolveRequest(), auth_context=EMPLOYEE)
    assert response["resolution"]["action_type"] == "permission_grant", response["resolution"]
    assert response["risk_decision"]["decision"] == "require_approval", response["risk_decision"]
    assert _mcp_calls(*IT_ACTION_TOOLS) == before, "a grant must not run before the human decides"


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _it_tickets() -> list[dict]:
    """The tickets this script filed. ``it_category`` is what intake stamps."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id FROM tickets WHERE it_category IS NOT NULL ORDER BY id ASC"
        ).fetchall()
    return rows_to_dicts(rows)


def _approvals_for_run(workflow_run_id: str | None) -> list[dict]:
    if not workflow_run_id:
        return []
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


def _mcp_calls(*tool_names: str) -> int:
    """How many times these tools were actually invoked, not merely attempted."""
    return len([entry for entry in _audit("mcp.tool_call") if entry["target_id"] in tool_names])


if __name__ == "__main__":
    main()
