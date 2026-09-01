from __future__ import annotations

import json
import logging

from app.config import settings
from app.services.agent.executor import get_run_detail, run_workflow
from app.services.agent.planner import plan_workflow
from app.services.llm import LLMError, complete_json, llm_ready
from app.services.multi_agent.memory import add_memory, search_similar_memories
from app.services.tools.knowledge import query_enterprise_rag, search_knowledge


logger = logging.getLogger("agent_platform.multi_agent")
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
SIDE_EFFECT_TOOLS = {
    "create_ticket",
    "update_ticket",
    "send_email",
    "notify_internal_team",
    "request_approval",
}


class SupervisorAgent:
    name = "supervisor"

    def run(self, objective: str, memory_context: dict | None = None) -> dict:
        plan = plan_workflow(objective).__dict__
        research_required = plan["category"] not in {"ticket_query", "ticket_update"}
        required_agents = ["risk_approval", "tool_execution", "critic", "memory"]
        if research_required:
            required_agents.insert(0, "rag_research")

        similar_cases = list((memory_context or {}).get("similar_cases") or [])
        failure_patterns = [item for item in similar_cases if item.get("memory_type") == "failure_pattern"]
        memory_constraints: list[str] = []
        for item in failure_patterns:
            findings = (item.get("detail") or {}).get("critic_report", {}).get("findings", [])
            for finding in findings:
                code = str(finding.get("code") or "").strip()
                if code and code not in memory_constraints:
                    memory_constraints.append(code)
        task_graph = []
        if research_required:
            task_graph.extend(
                [
                    {
                        "task_key": "research.enterprise_rag",
                        "agent": "enterprise_rag_research",
                        "depends_on": ["plan.supervisor"],
                        "description": "Retrieve ACL-filtered evidence and citations from the enterprise RAG service.",
                    },
                    {
                        "task_key": "research.local_policy",
                        "agent": "local_policy_research",
                        "depends_on": ["plan.supervisor"],
                        "description": "Retrieve independent fallback evidence from the local policy store.",
                    },
                    {
                        "task_key": "research.synthesis",
                        "agent": "rag_research",
                        "depends_on": ["research.enterprise_rag", "research.local_policy"],
                        "description": "Reconcile both evidence channels and publish one shared evidence package.",
                    },
                ]
            )
        risk_dependency = ["research.synthesis"] if research_required else ["plan.supervisor"]
        task_graph.extend(
            [
                {
                    "task_key": "risk.compliance_vote",
                    "agent": "compliance_risk",
                    "depends_on": risk_dependency,
                    "description": "Independently assess policy, evidence, approval, and compliance risk.",
                },
                {
                    "task_key": "risk.operational_vote",
                    "agent": "operational_risk",
                    "depends_on": risk_dependency,
                    "description": "Independently assess reversibility, external side effects, and business impact.",
                },
                {
                    "task_key": "risk.consensus",
                    "agent": "risk_approval",
                    "depends_on": ["risk.compliance_vote", "risk.operational_vote"],
                    "description": "Aggregate risk votes conservatively; any escalation or approval vote wins.",
                },
            ]
        )
        task_graph.extend(
            [
                {
                    "task_key": "action.execute",
                    "agent": "tool_execution",
                    "depends_on": ["risk.consensus"],
                    "description": "Execute the approved plan using the shared plan, evidence, and risk decision.",
                },
                {
                    "task_key": "quality.critic",
                    "agent": "critic",
                    "depends_on": ["action.execute"],
                    "description": "Verify coordination handoffs, evidence use, approval state, and tool outcomes.",
                },
                {
                    "task_key": "memory.write",
                    "agent": "memory",
                    "depends_on": ["quality.critic"],
                    "description": "Persist the successful pattern or failure findings for future planning.",
                },
            ]
        )
        return {
            "plan": plan,
            "required_agents": required_agents,
            "research_required": research_required,
            "task_graph": task_graph,
            "memory_influence": {
                "similar_case_count": len(similar_cases),
                "failure_constraints": memory_constraints,
                "risk_constraints_applied": bool(memory_constraints),
            },
            "routing_reason": (
                "Always run independent research and risk votes; category, approval policy, and prior failure "
                "patterns influence the expert inputs and conservative consensus."
            ),
        }


class EnterpriseRagResearchAgent:
    name = "enterprise_rag_research"

    def run(
        self,
        objective: str,
        *,
        user_id: str | None = None,
        user_department: str | None = None,
        user_role: str | None = None,
    ) -> dict:
        return query_enterprise_rag(
            objective,
            top_k=3,
            user_id=user_id,
            user_department=user_department,
            user_role=user_role,
        )


class LocalPolicyResearchAgent:
    name = "local_policy_research"

    def run(self, objective: str) -> dict:
        return search_knowledge(objective, limit=3)


class RagResearchAgent:
    name = "rag_research"

    def run(
        self,
        objective: str,
        enterprise_rag: dict | None = None,
        local_policy: dict | None = None,
        *,
        user_id: str | None = None,
        user_department: str | None = None,
        user_role: str | None = None,
    ) -> dict:
        rag = enterprise_rag or EnterpriseRagResearchAgent().run(
            objective,
            user_id=user_id,
            user_department=user_department,
            user_role=user_role,
        )
        local = local_policy or LocalPolicyResearchAgent().run(objective)
        enterprise_results = list(rag.get("results") or [])
        local_results = list(local.get("results") or [])
        evidence = _merge_evidence(enterprise_results, local_results)
        has_enterprise = bool(rag.get("available") and enterprise_results)
        has_local = bool(local_results)
        if has_enterprise and has_local:
            selected_source = "enterprise_rag+local_policy_db"
        elif has_enterprise:
            selected_source = "enterprise_rag"
        else:
            selected_source = "local_policy_db"
        warnings = []
        if not rag.get("available"):
            warnings.append("enterprise_rag_unavailable")
        if not evidence:
            warnings.append("no_relevant_policy_evidence")
        llm_synthesis = _expert_json(
            "evidence_synthesis",
            (
                "You are an enterprise evidence synthesis agent. Compare the supplied evidence channels, "
                "identify conflicts or gaps, and summarize only supported facts. Return JSON with "
                "summary, conflicts (array), and confidence (0..1). Never invent evidence."
            ),
            {
                "objective": objective,
                "enterprise_available": bool(rag.get("available")),
                "enterprise_can_answer": bool(rag.get("can_answer")),
                "evidence": evidence[:6],
            },
        )
        return {
            "enterprise_rag": rag,
            "local_policy": local,
            "selected_source": selected_source,
            "evidence": evidence,
            "evidence_count": len(evidence),
            "citation_count": len(rag.get("citations") or []),
            "can_answer": bool(rag.get("can_answer") or local_results),
            "warnings": warnings,
            "synthesis_summary": str((llm_synthesis or {}).get("summary") or "").strip(),
            "conflicts": _string_list((llm_synthesis or {}).get("conflicts"), limit=5),
            "reasoning_mode": "llm_augmented" if llm_synthesis else "deterministic_evidence_merge",
            "producer": self.name,
            "handoff_contract": {
                "consumer": "tool_execution",
                "fields": ["enterprise_rag", "local_policy", "evidence", "selected_source", "evidence_count"],
                "reuse_without_retrieval": True,
            },
        }


class ComplianceRiskAgent:
    name = "compliance_risk"

    def run(self, supervisor_output: dict, research_output: dict) -> dict:
        plan = supervisor_output["plan"]
        warnings = []
        if plan["needs_approval"]:
            warnings.append("human_approval_required")
        if not research_output["enterprise_rag"].get("available") and not research_output["enterprise_rag"].get("skipped"):
            warnings.append("enterprise_rag_unavailable")
        if plan["category"] in {"security", "access_request", "refund", "procurement"}:
            warnings.append("sensitive_business_category")
        if plan.get("approval_chain"):
            warnings.append(f"approval_chain:{'->'.join(plan['approval_chain'])}")
        memory_constraints = list(
            (supervisor_output.get("memory_influence") or {}).get("failure_constraints") or []
        )
        warnings.extend(f"historical_failure:{code}" for code in memory_constraints)
        historical_escalation = any(
            code
            in {
                "approval_state_wrong",
                "missing_approval_tool",
                "missing_research_handoff",
                "missing_supervisor_handoff",
                "workflow_failed",
            }
            for code in memory_constraints
        )
        vote = {
            "voter": self.name,
            "risk_level": plan["risk_level"],
            "needs_approval": bool(plan["needs_approval"] or historical_escalation),
            "warnings": list(dict.fromkeys(warnings)),
            "reason": "Policy and evidence sufficiency vote.",
            "reasoning_mode": "deterministic_policy",
        }
        suggestion = _expert_json(
            self.name,
            (
                "You are an independent enterprise compliance risk agent. Review policy evidence, approval "
                "requirements, ACL context, and historical failures. Return JSON with risk_level, "
                "needs_approval, warnings (array), reason, confidence. You may only escalate risk; never "
                "weaken a required approval."
            ),
            {
                "plan": plan,
                "research": _research_digest(research_output),
                "historical_failure_constraints": memory_constraints,
            },
        )
        vote = _apply_expert_risk_suggestion(vote, suggestion)
        vote["vote"] = "require_approval" if vote["needs_approval"] else "allow"
        return vote


class OperationalRiskAgent:
    name = "operational_risk"

    def run(self, supervisor_output: dict, research_output: dict) -> dict:
        plan = supervisor_output["plan"]
        category = plan["category"]
        external_side_effect = bool(plan.get("recipient_email")) or category in {
            "refund",
            "procurement",
            "security",
            "access_request",
            "incident",
        }
        evidence_missing = (
            research_output.get("selected_source") != "not_required"
            and int(research_output.get("evidence_count") or 0) == 0
        )
        needs_approval = bool(plan["needs_approval"] or (external_side_effect and evidence_missing))
        risk_level = plan["risk_level"]
        if evidence_missing and external_side_effect and risk_level == "low":
            risk_level = "medium"
        warnings = []
        if external_side_effect:
            warnings.append("external_or_irreversible_side_effect")
        if evidence_missing:
            warnings.append("no_policy_evidence")
        memory_constraints = list(
            (supervisor_output.get("memory_influence") or {}).get("failure_constraints") or []
        )
        repeated_execution_failure = any(code in {"failed_steps", "workflow_failed"} for code in memory_constraints)
        if repeated_execution_failure:
            needs_approval = True
            if RISK_ORDER.get(risk_level, 0) < RISK_ORDER["medium"]:
                risk_level = "medium"
            warnings.append("historical_execution_failure")
        vote = {
            "voter": self.name,
            "risk_level": risk_level,
            "needs_approval": needs_approval,
            "warnings": warnings,
            "reason": "Operational reversibility, evidence, and external-impact vote.",
            "reasoning_mode": "deterministic_policy",
        }
        suggestion = _expert_json(
            self.name,
            (
                "You are an independent operational risk agent. Review reversibility, external side effects, "
                "business impact, evidence gaps, and historical execution failures. Return JSON with "
                "risk_level, needs_approval, warnings (array), reason, confidence. You may only escalate "
                "risk; never remove an approval requirement."
            ),
            {
                "plan": plan,
                "research": _research_digest(research_output),
                "historical_failure_constraints": memory_constraints,
            },
        )
        vote = _apply_expert_risk_suggestion(vote, suggestion)
        vote["vote"] = "require_approval" if vote["needs_approval"] else "allow"
        return vote


class RiskApprovalAgent:
    name = "risk_approval"

    def run(
        self,
        supervisor_output: dict,
        research_output: dict,
        compliance_vote: dict | None = None,
        operational_vote: dict | None = None,
    ) -> dict:
        plan = supervisor_output["plan"]
        votes = [vote for vote in [compliance_vote, operational_vote] if vote]
        if not votes:
            votes = [ComplianceRiskAgent().run(supervisor_output, research_output)]
        risk_level = max(
            [plan["risk_level"], *(str(vote.get("risk_level") or "low") for vote in votes)],
            key=lambda value: RISK_ORDER.get(value, 0),
        )
        needs_approval = bool(plan["needs_approval"] or any(vote.get("needs_approval") for vote in votes))
        warnings = list(dict.fromkeys(str(item) for vote in votes for item in (vote.get("warnings") or [])))
        return {
            "risk_level": risk_level,
            "needs_approval": needs_approval,
            "category": plan["category"],
            "workflow_type": plan.get("workflow_type"),
            "blocked_actions": plan.get("blocked_actions", []),
            "warnings": warnings,
            "decision": "pause_for_approval" if needs_approval else "auto_execute",
            "votes": votes,
            "consensus_rule": "max_risk_and_any_approval_vote",
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
        supervisor_output: dict | None = None,
        research_output: dict | None = None,
        risk_output: dict | None = None,
    ) -> dict:
        workflow = run_workflow(
            objective,
            requester_user_id=requester_user_id,
            requester_department=requester_department,
            requester_role=requester_role,
            tenant_id=tenant_id,
            plan_override=(supervisor_output or {}).get("plan"),
            knowledge_override=research_output,
            risk_override=risk_output,
        )
        approval_steps = [
            step for step in workflow.get("steps", []) if step.get("tool_name") == "request_approval"
        ]
        approval_id = None
        if approval_steps:
            approval_id = (approval_steps[-1].get("tool_output") or {}).get("id")
        return {
            "workflow_run_id": workflow["id"],
            "workflow_status": workflow["status"],
            "category": workflow.get("category"),
            "risk_level": workflow.get("risk_level"),
            "needs_approval": bool(workflow.get("needs_approval")),
            "step_count": len(workflow.get("steps", [])),
            "approval_id": approval_id,
            "final_answer": workflow.get("final_answer"),
            "coordination": {
                "supervisor_plan_consumed": bool(supervisor_output),
                "shared_evidence_consumed": bool(research_output),
                "synthesized_evidence_consumed": isinstance(research_output, dict) and "evidence" in research_output,
                "risk_consensus_consumed": bool(risk_output),
            },
        }

    def summarize_workflow(self, workflow: dict) -> dict:
        return {
            "workflow_run_id": workflow["id"],
            "workflow_status": workflow["status"],
            "category": workflow.get("category"),
            "risk_level": workflow.get("risk_level"),
            "needs_approval": bool(workflow.get("needs_approval")),
            "step_count": len(workflow.get("steps", [])),
            "final_answer": workflow.get("final_answer"),
            "approval_resumed": True,
            "coordination": {
                "supervisor_plan_consumed": True,
                "shared_evidence_consumed": True,
                "synthesized_evidence_consumed": True,
                "risk_consensus_consumed": True,
            },
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
        approval_denied = bool(
            workflow
            and workflow.get("status") == "cancelled"
            and "request_approval" in tools
        )
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
            if "create_ticket" not in tools and not waiting_for_approval and not approval_denied:
                findings.append({"severity": "high", "code": "missing_ticket", "message": "No operational ticket was created."})
                score -= 25
        if supervisor_output["plan"]["needs_approval"] and workflow.get("status") not in {"waiting_approval", "completed", "cancelled"}:
            findings.append({"severity": "high", "code": "approval_state_wrong", "message": "Risky workflow did not pause or complete through approval path."})
            score -= 20
        if risk_output and risk_output["needs_approval"] and "request_approval" not in tools:
            findings.append({"severity": "medium", "code": "missing_approval_tool", "message": "Risk agent expected approval but approval tool was not called."})
            score -= 15
        if category in {"security", "access_request", "incident"} and "notify_internal_team" not in tools and not approval_denied:
            findings.append({"severity": "medium", "code": "missing_internal_notification", "message": "Security, access, or incident workflow should notify the responsible internal team."})
            score -= 15
        if category not in {"refund", "complaint", "communication"} and "send_email" in tools:
            findings.append({"severity": "high", "code": "unexpected_external_email", "message": "Non-customer workflow sent an external email."})
            score -= 25
        failed_steps = [step for step in steps if step.get("status") == "failed"]
        if failed_steps:
            findings.append({"severity": "medium", "code": "failed_steps", "message": f"{len(failed_steps)} step(s) failed."})
            score -= 10
        if workflow and workflow.get("status") == "failed":
            findings.append(
                {
                    "severity": "critical",
                    "code": "workflow_failed",
                    "message": "The delegated operational workflow failed and cannot pass the quality gate.",
                }
            )
            score -= 40
        if workflow and workflow.get("refusal_reason") == "prompt_injection_or_unsafe_instruction":
            findings.append(
                {
                    "severity": "critical",
                    "code": "prompt_injection_or_unsafe_instruction",
                    "message": "The workflow guard refused an unsafe or approval-bypass instruction.",
                }
            )
            score -= 40
        retry_attempts = sum(max(0, int(step.get("attempt_count") or 1) - 1) for step in steps)

        coordination = next(
            (
                step.get("tool_input", {}).get("coordination")
                for step in steps
                if step.get("node_name") == "plan" and step.get("tool_input")
            ),
            {},
        ) or {}
        if not coordination.get("supervisor_plan_consumed"):
            findings.append(
                {
                    "severity": "high",
                    "code": "missing_supervisor_handoff",
                    "message": "Tool execution did not consume the supervisor plan.",
                }
            )
            score -= 20
        evidence_required = category not in {"ticket_query", "ticket_update"}
        if evidence_required and not coordination.get("shared_evidence_consumed"):
            findings.append(
                {
                    "severity": "high",
                    "code": "missing_research_handoff",
                    "message": "Tool execution did not consume the research agents' shared evidence.",
                }
            )
            score -= 20
        if evidence_required and not coordination.get("synthesized_evidence_consumed"):
            findings.append(
                {
                    "severity": "high",
                    "code": "missing_synthesized_evidence_handoff",
                    "message": "Tool execution did not consume the evidence package published by the synthesis agent.",
                }
            )
            score -= 20

        successful_side_effect_tools = sorted(
            {
                str(step.get("tool_name"))
                for step in steps
                if step.get("tool_name") in SIDE_EFFECT_TOOLS and step.get("status") == "completed"
            }
        )
        llm_review = _expert_json(
            self.name,
            (
                "You are an independent quality critic for an enterprise agent workflow. Find only concrete "
                "gaps supported by the supplied trace. Return JSON with findings, where each finding has "
                "severity, code, and message. Do not mark the run as passed and do not invent tool calls."
            ),
            {
                "plan": supervisor_output.get("plan"),
                "risk_consensus": risk_output or {},
                "workflow_status": workflow.get("status") if workflow else "missing",
                "steps": [
                    {
                        "node": step.get("node_name"),
                        "tool": step.get("tool_name"),
                        "status": step.get("status"),
                        "error_type": step.get("error_type"),
                    }
                    for step in steps
                ],
            },
        )
        llm_findings = _valid_critic_findings((llm_review or {}).get("findings"))
        existing_codes = {str(finding.get("code")) for finding in findings}
        severity_penalty = {"low": 3, "medium": 7, "high": 12, "critical": 20}
        for finding in llm_findings:
            if finding["code"] in existing_codes or finding["code"].removeprefix("llm_") in existing_codes:
                continue
            findings.append(finding)
            existing_codes.add(finding["code"])
            score -= severity_penalty[finding["severity"]]

        return {
            "score": max(0, score),
            "passed": score >= 80,
            "findings": findings,
            "tool_sequence": tools,
            "retry_attempts": retry_attempts,
            "workflow_status": workflow.get("status") if workflow else "missing",
            "approval_denied_safely": approval_denied,
            "successful_side_effect_tools": successful_side_effect_tools,
            "safe_to_retry": not successful_side_effect_tools,
            "reasoning_mode": "llm_augmented" if llm_review else "deterministic_quality_gate",
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

        unsafe_request = any(finding.get("code") == "prompt_injection_or_unsafe_instruction" for finding in findings)
        side_effects = list(critic_report.get("successful_side_effect_tools") or [])
        safe_to_retry = bool(critic_report.get("safe_to_retry", not side_effects))
        blocked_reason = None
        if unsafe_request:
            blocked_reason = "unsafe_request_cannot_be_replayed"
        elif not safe_to_retry:
            blocked_reason = "successful_side_effects_require_human_review"
        should_retry = bool(additions) and blocked_reason is None
        corrected_objective = objective
        if should_retry:
            corrected_objective = f"{objective}\n\nSelf-correction requirements:\n- " + "\n- ".join(additions)
        return {
            "should_retry": should_retry,
            "corrected_objective": corrected_objective,
            "corrections": additions,
            "blocked_reason": blocked_reason,
            "successful_side_effect_tools": side_effects,
            "requires_human_review": blocked_reason == "successful_side_effects_require_human_review",
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


def _expert_json(agent_name: str, system_prompt: str, payload: dict) -> dict | None:
    if not settings.llm_multi_agent_reasoning_enabled or not llm_ready():
        return None
    try:
        return complete_json(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=0,
            max_tokens=600,
        )
    except LLMError as exc:
        logger.warning(
            "multi_agent.expert_reasoning_failed",
            extra={"event": "multi_agent.expert_reasoning_failed", "agent": agent_name, "error": str(exc)},
        )
        return None


def _merge_evidence(enterprise_results: list[dict], local_results: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in [*enterprise_results, *local_results]:
        source = str(item.get("source") or item.get("category") or "unknown")
        identity = str(
            item.get("chunk_id")
            or item.get("article_id")
            or item.get("document_id")
            or item.get("title")
            or item.get("snippet")
            or ""
        )
        key = (source, identity)
        if not identity or key in seen:
            continue
        seen.add(key)
        merged.append(
            {
                "source": source,
                "title": str(item.get("title") or "Policy evidence"),
                "snippet": str(item.get("snippet") or item.get("content") or "")[:320],
                "score": item.get("score"),
                "document_id": item.get("document_id"),
                "chunk_id": item.get("chunk_id"),
                "article_id": item.get("article_id"),
                "page_number": item.get("page_number"),
                "section_title": item.get("section_title"),
            }
        )
    return merged[:10]


def _research_digest(research_output: dict) -> dict:
    enterprise = research_output.get("enterprise_rag") or {}
    return {
        "selected_source": research_output.get("selected_source"),
        "evidence_count": int(research_output.get("evidence_count") or 0),
        "citation_count": int(research_output.get("citation_count") or len(enterprise.get("citations") or [])),
        "enterprise_available": bool(enterprise.get("available")),
        "enterprise_can_answer": bool(enterprise.get("can_answer")),
        "warnings": list(research_output.get("warnings") or []),
        "conflicts": list(research_output.get("conflicts") or []),
        "evidence": list(research_output.get("evidence") or [])[:5],
    }


def _apply_expert_risk_suggestion(vote: dict, suggestion: dict | None) -> dict:
    if not suggestion:
        return vote
    result = dict(vote)
    suggested_risk = str(suggestion.get("risk_level") or "").lower()
    current_risk = str(result.get("risk_level") or "low")
    if suggested_risk in RISK_ORDER and RISK_ORDER[suggested_risk] > RISK_ORDER.get(current_risk, 0):
        result["risk_level"] = suggested_risk
    result["needs_approval"] = bool(result.get("needs_approval") or suggestion.get("needs_approval"))
    result["warnings"] = list(
        dict.fromkeys([*list(result.get("warnings") or []), *_string_list(suggestion.get("warnings"), limit=8)])
    )
    reason = str(suggestion.get("reason") or "").strip()
    if reason:
        result["expert_reason"] = reason[:500]
    confidence = suggestion.get("confidence")
    if isinstance(confidence, (int, float)):
        result["confidence"] = max(0.0, min(float(confidence), 1.0))
    result["reasoning_mode"] = "llm_augmented"
    return result


def _string_list(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip()[:240] for item in value if str(item).strip()][:limit]


def _valid_critic_findings(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    findings = []
    for raw in value[:8]:
        if not isinstance(raw, dict):
            continue
        severity = str(raw.get("severity") or "").lower()
        code = str(raw.get("code") or "").strip().lower().replace(" ", "_")[:80]
        message = str(raw.get("message") or "").strip()[:500]
        if severity not in {"low", "medium", "high", "critical"} or not code or not message:
            continue
        findings.append({"severity": severity, "code": f"llm_{code}", "message": message})
    return findings
