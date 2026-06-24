from __future__ import annotations

from app.services.agent.planner import plan_workflow
from app.services.agent.executor import get_run_detail, run_workflow
from app.services.multi_agent.memory import add_memory, search_similar_memories
from app.services.tools.knowledge import query_enterprise_rag, search_knowledge


class SupervisorAgent:
    name = "supervisor"

    def run(self, objective: str) -> dict:
        plan = plan_workflow(objective).__dict__
        required_agents = ["rag_research", "risk_approval", "tool_execution", "critic", "memory"]
        if plan["risk_level"] == "low":
            required_agents = ["rag_research", "tool_execution", "critic", "memory"]
        return {
            "plan": plan,
            "required_agents": required_agents,
            "routing_reason": "Route based on deterministic category, risk level, and approval requirement.",
        }


class RagResearchAgent:
    name = "rag_research"

    def run(
        self,
        objective: str,
        *,
        user_id: str | None = None,
        user_department: str | None = None,
        user_role: str | None = None,
    ) -> dict:
        rag = query_enterprise_rag(
            objective,
            top_k=3,
            user_id=user_id,
            user_department=user_department,
            user_role=user_role,
        )
        local = search_knowledge(objective, limit=3)
        return {
            "enterprise_rag": rag,
            "local_policy": local,
            "selected_source": "enterprise_rag" if rag.get("available") and rag.get("results") else "local_policy_db",
            "evidence_count": len(rag.get("results", [])) + len(local.get("results", [])),
        }


class RiskApprovalAgent:
    name = "risk_approval"

    def run(self, supervisor_output: dict, research_output: dict) -> dict:
        plan = supervisor_output["plan"]
        warnings = []
        if plan["needs_approval"]:
            warnings.append("human_approval_required")
        if not research_output["enterprise_rag"].get("available"):
            warnings.append("enterprise_rag_unavailable")
        if plan["category"] in {"security", "access_request", "refund", "procurement"}:
            warnings.append("sensitive_business_category")
        if plan.get("approval_chain"):
            warnings.append(f"approval_chain:{'->'.join(plan['approval_chain'])}")
        return {
            "risk_level": plan["risk_level"],
            "needs_approval": plan["needs_approval"],
            "category": plan["category"],
            "workflow_type": plan.get("workflow_type"),
            "blocked_actions": plan.get("blocked_actions", []),
            "warnings": warnings,
            "decision": "pause_for_approval" if plan["needs_approval"] else "auto_execute",
        }


class ToolExecutionAgent:
    name = "tool_execution"

    def run(
        self,
        objective: str,
        *,
        requester_user_id: str | None = None,
        requester_department: str | None = None,
        requester_role: str | None = None,
        tenant_id: str | None = None,
    ) -> dict:
        workflow = run_workflow(
            objective,
            requester_user_id=requester_user_id,
            requester_department=requester_department,
            requester_role=requester_role,
            tenant_id=tenant_id,
        )
        return {
            "workflow_run_id": workflow["id"],
            "workflow_status": workflow["status"],
            "category": workflow.get("category"),
            "risk_level": workflow.get("risk_level"),
            "needs_approval": bool(workflow.get("needs_approval")),
            "step_count": len(workflow.get("steps", [])),
        }


class CriticAgent:
    name = "critic"

    def run(self, workflow_run_id: str, supervisor_output: dict, risk_output: dict | None) -> dict:
        workflow = get_run_detail(workflow_run_id)
        steps = workflow.get("steps", []) if workflow else []
        tools = [step.get("tool_name") for step in steps if step.get("tool_name")]
        findings = []
        score = 100

        category = supervisor_output["plan"].get("category")
        if category == "ticket_query":
            if "query_tickets" not in tools:
                findings.append({"severity": "high", "code": "missing_ticket_query", "message": "Ticket query tool was not called."})
                score -= 25
        elif category == "ticket_update":
            if "update_ticket" not in tools:
                findings.append({"severity": "high", "code": "missing_ticket_update", "message": "Ticket update tool was not called."})
                score -= 25
        else:
            if "query_enterprise_rag" not in tools:
                findings.append({"severity": "high", "code": "missing_rag_tool", "message": "RAG tool was not called."})
                score -= 25
            waiting_for_approval = bool(supervisor_output["plan"].get("needs_approval")) and workflow.get("status") == "waiting_approval"
            if "create_ticket" not in tools and not waiting_for_approval:
                findings.append({"severity": "high", "code": "missing_ticket", "message": "No operational ticket was created."})
                score -= 25
        if supervisor_output["plan"]["needs_approval"] and workflow.get("status") not in {"waiting_approval", "completed"}:
            findings.append({"severity": "high", "code": "approval_state_wrong", "message": "Risky workflow did not pause or complete through approval path."})
            score -= 20
        if risk_output and risk_output["needs_approval"] and "request_approval" not in tools:
            findings.append({"severity": "medium", "code": "missing_approval_tool", "message": "Risk agent expected approval but approval tool was not called."})
            score -= 15
        if category in {"security", "access_request", "incident"} and "notify_internal_team" not in tools:
            findings.append({"severity": "medium", "code": "missing_internal_notification", "message": "Security, access, or incident workflow should notify the responsible internal team."})
            score -= 15
        if category not in {"refund", "complaint", "communication"} and "send_email" in tools:
            findings.append({"severity": "high", "code": "unexpected_external_email", "message": "Non-customer workflow sent an external email."})
            score -= 25
        failed_steps = [step for step in steps if step.get("status") == "failed"]
        if failed_steps:
            findings.append({"severity": "medium", "code": "failed_steps", "message": f"{len(failed_steps)} step(s) failed."})
            score -= 10
        retry_attempts = sum(max(0, int(step.get("attempt_count") or 1) - 1) for step in steps)

        return {
            "score": max(0, score),
            "passed": score >= 80,
            "findings": findings,
            "tool_sequence": tools,
            "retry_attempts": retry_attempts,
            "workflow_status": workflow.get("status") if workflow else "missing",
        }


class CorrectionAgent:
    name = "self_correction"

    def run(self, objective: str, critic_report: dict, supervisor_output: dict | None = None) -> dict:
        findings = critic_report.get("findings", [])
        codes = {finding.get("code") for finding in findings}
        additions = []
        if "missing_rag_tool" in codes:
            additions.append("Check enterprise RAG and local policy evidence before executing.")
        if "missing_ticket" in codes:
            additions.append("Create an auditable operational ticket.")
        if "missing_approval_tool" in codes or "approval_state_wrong" in codes:
            additions.append("If the action is high risk, pause and request human approval.")
        if "failed_steps" in codes:
            additions.append("Retry only retryable tools and preserve audit evidence.")
        if not additions and critic_report.get("score", 100) < 80:
            additions.append("Re-run with full tool evidence, ticket creation, and approval checks.")

        blocked = any(finding.get("code") == "prompt_injection_or_unsafe_instruction" for finding in findings)
        should_retry = bool(additions) and not blocked
        corrected_objective = objective
        if should_retry:
            corrected_objective = f"{objective}\n\nSelf-correction requirements:\n- " + "\n- ".join(additions)
        return {
            "should_retry": should_retry,
            "corrected_objective": corrected_objective,
            "corrections": additions,
            "original_score": critic_report.get("score", 0),
            "supervisor_category": (supervisor_output or {}).get("plan", {}).get("category"),
        }


class MemoryAgent:
    name = "memory"

    def retrieve(self, objective: str, *, tenant_id: str | None = None) -> dict:
        return {"similar_cases": search_similar_memories(objective, limit=3, tenant_id=tenant_id)}

    def write(
        self,
        multi_agent_run_id: str,
        objective: str,
        critic_report: dict,
        workflow_run_id: str,
        *,
        tenant_id: str | None = None,
    ) -> dict:
        memory_type = "success_case" if critic_report.get("passed") else "failure_pattern"
        summary = f"{memory_type}: score={critic_report.get('score')} workflow={workflow_run_id}"
        return add_memory(
            memory_type,
            memory_key=objective[:120],
            summary=summary,
            detail={"critic_report": critic_report, "workflow_run_id": workflow_run_id},
            tenant_id=tenant_id,
            source_run_id=multi_agent_run_id,
            score=float(critic_report.get("score") or 0),
        )
