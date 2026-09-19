from __future__ import annotations

import json
import logging

from app.config import settings
from app.services.agent.executor import get_run_detail, run_workflow
from app.services.agent.planner import plan_workflow
from app.services.it.risk_gate import ACTION_CLASSES
from app.services.it.triage import CRITICAL_SERVICES
from app.services.llm import LlmOutcome, call_json, llm_ready
from app.services.llm_schemas import (
    CriticReviewResult,
    EvidenceSynthesisResult,
    RiskVoteSuggestion,
    RunbookCandidate,
)
from app.services.llm_telemetry import record_llm_call
from app.services.multi_agent.memory import add_memory, search_similar_memories
from app.services.tools.knowledge import query_enterprise_rag, search_knowledge
from app.services.tools.registry import call_tool


logger = logging.getLogger("agent_platform.multi_agent")
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
SIDE_EFFECT_TOOLS = {
    "create_ticket",
    "update_ticket",
    "send_email",
    "notify_internal_team",
    "request_approval",
}

# --- IT resolution -----------------------------------------------------------
# A resolution with no retrieved evidence is not a plan, it is a guess, so one
# quotable evidence item is the floor for proposing anything at all.
MIN_EVIDENCE_ITEMS = 1

RESTART_KEYWORDS: tuple[str, ...] = ("重启", "restart", "恢复", "重新启动", "reboot")
CACHE_KEYWORDS: tuple[str, ...] = ("缓存", "cache", "flush", "清理")

# Fallback when neither the request nor the evidence points at an action:
# read-only diagnostics, which cannot make anything worse.
INCIDENT_DEFAULT_ACTION = "diagnostic_read"


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


class HistoricalTicketResearchAgent:
    """Retrieve how comparable incidents were handled in the past.

    This is the second evidence channel and it stays strictly second. What comes
    back is a record of what somebody once did, not an approved procedure, so the
    caller stores it under ``historical_*`` keys that the resolution agent reads
    for reporting and never for choosing an action.

    Unlike its two siblings above it reaches the corpus through ``call_tool``
    rather than importing the function. That costs one role-gate lookup and buys
    two things the direct import cannot: the retrieval appears as an
    ``mcp.tool_call`` row like every other tool invocation, and there is no way
    to read the corpus that bypasses the registry.
    """

    name = "historical_ticket_research"

    def run(self, objective: str, *, tenant_id: str | None = None, limit: int = 3) -> dict:
        return call_tool(
            "search_historical_tickets",
            {"query": objective, "limit": limit, "tenant_id": tenant_id},
            source="multi_agent_it",
        )


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
        synthesis = _expert_call(
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
            operation="evidence_synthesis",
            schema=EvidenceSynthesisResult,
        )
        llm_synthesis = synthesis.value if synthesis.ok else None
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
            "reasoning_mode": _reasoning_mode(
                synthesis, deterministic="deterministic_evidence_merge", augmented="llm_augmented"
            ),
            "llm": _llm_trace(synthesis),
            "producer": self.name,
            "handoff_contract": {
                "consumer": "tool_execution",
                "fields": ["enterprise_rag", "local_policy", "evidence", "selected_source", "evidence_count"],
                "reuse_without_retrieval": True,
            },
        }


class ResolutionAgent:
    """Turn evidence + triage + ticket context into one proposed IT action.

    This agent **proposes and never executes**. It returns a structured
    resolution; whether anything runs is decided afterwards by the
    deterministic risk gate and, when the gate asks for one, a human. Nothing
    in this class calls a mutating tool.

    Two rules shape the design:

    * **No evidence, no answer.** If retrieval came back empty the agent
      returns ``NO_KNOWLEDGE`` with ``diagnosis: None`` rather than letting a
      model improvise a plausible-sounding fix. The check is deterministic and
      runs before any LLM call, so it cannot be talked out of.
    * **The LLM may phrase, never decide.** The action type, target and
      arguments are derived by the code below. An LLM is at most allowed to
      reword the diagnosis, and its ``action_type`` is discarded outright.
      The same discipline as ``_apply_expert_risk_suggestion``, which can only
      escalate risk and never remove an approval.

    ``confidence`` is reported for the audit trail and is never read by the
    risk gate.

    Two evidence channels arrive here and they are not equals. ``research_output``
    is formal knowledge — approved policy and runbooks — and it alone decides
    whether an action may be proposed and which one. ``historical_output`` is
    what happened on comparable tickets before, and it is reported under its own
    ``historical_*`` keys without ever reaching ``_select_action``: a historical
    ticket naming a class the gate denies cannot move the selected action, and
    that is a property of the code (`evidence` is built from one input only)
    rather than a promise made in a prompt.
    """

    name = "resolution"

    def run(
        self,
        objective: str,
        *,
        triage: dict | None = None,
        research_output: dict | None = None,
        ticket: dict | None = None,
        historical_output: dict | None = None,
    ) -> dict:
        triage = triage or {}
        research_output = research_output or {}
        ticket = ticket or {}
        entities = dict(triage.get("entities") or {})
        environment = _first_text(ticket.get("environment"), entities.get("environment"))
        # Knowledge only. The historical channel is read below and deliberately
        # never enters this variable — this line is the whole separation.
        evidence = _resolution_evidence(research_output)
        historical = _historical_evidence(historical_output)
        missing_information = [str(item) for item in (triage.get("missing_information") or [])]

        if not _sufficient_evidence(research_output, evidence):
            return self._no_knowledge(
                triage, research_output, environment, missing_information, historical
            )

        intent = str(triage.get("intent") or "IT_INCIDENT")
        action_type, reason = _select_action(objective, intent, entities, environment, evidence)
        action_class = ACTION_CLASSES[action_type]
        arguments = _action_arguments(action_type, ticket, entities, triage)
        target = _first_text(ticket.get("asset_id"), entities.get("resource"), entities.get("service"))

        confidence = _resolution_confidence(evidence)
        resolution = {
            "status": "PROPOSED",
            "diagnosis": _diagnosis(evidence),
            "evidence": evidence[:3],
            "evidence_count": len(evidence),
            "proposed_action": action_class.tool_name,
            "action_type": action_type,
            "action_arguments": arguments,
            "target": target,
            "environment": environment,
            "confidence": confidence,
            # The agent's opinion, recorded for the audit trail. The gate below
            # is what actually decides; see app.services.it.risk_gate.
            "requires_approval": bool(
                triage.get("needs_approval") or action_class.requires_approval
            ),
            "missing_information": missing_information,
            "reason": reason,
            "mode": "deterministic",
            "producer": self.name,
            "handoff_contract": {
                "consumer": "risk_gate",
                "fields": ["action_type", "action_arguments", "target", "environment", "evidence"],
                "advisory_only": ["confidence", "requires_approval"],
            },
            # Reference material, reported beside the evidence and consumed by
            # nothing downstream. ``historical_reference`` is false here on
            # purpose: the action was decided from knowledge, so the history is
            # illustrating the answer rather than standing in for one.
            **_historical_block(historical, referenced=False, chosen_action=action_type),
        }
        polish_outcome, polished = self._polish_diagnosis(objective, resolution)
        if polished:
            resolution["diagnosis"] = polished
            resolution["mode"] = "llm_augmented"
        elif polish_outcome.failed:
            # Wording was requested and did not arrive. The deterministic
            # diagnosis above is already complete, so this changes nothing a
            # reader acts on — but ``mode`` should not claim a rewrite that
            # never happened, and the audit trail should say why the phrasing
            # is the plain one.
            resolution["mode"] = "deterministic_llm_failed"
        candidates = self._runbook_candidates(objective, resolution)
        resolution["runbook_candidates"] = candidates["steps"]
        resolution["runbook_mode"] = candidates["mode"]
        resolution["llm"] = {
            "diagnosis_polish": _llm_trace(polish_outcome),
            "runbook_candidates": _llm_trace(candidates["outcome"]),
        }
        return resolution

    def _no_knowledge(
        self,
        triage: dict,
        research_output: dict,
        environment: str | None,
        missing_information: list[str],
        historical: list[dict] | None = None,
    ) -> dict:
        """Nothing formal was retrieved, so nothing is proposed — ever.

        Historical tickets do not change that. When some were found they are
        attached with ``historical_reference`` set, which is the flag that says
        "a human may find this useful" and simultaneously that it is *not* a
        policy basis: the status stays ``NO_KNOWLEDGE``, the diagnosis stays
        ``None`` and the action stays ``no_action``. The gate then refuses to run
        anything, so a missing runbook cannot be papered over by a past
        workaround, however similar it looks.
        """
        historical = historical or []
        return {
            "status": "NO_KNOWLEDGE",
            "diagnosis": None,
            "evidence": [],
            "evidence_count": int(research_output.get("evidence_count") or 0),
            "proposed_action": None,
            "action_type": "no_action",
            "action_arguments": {},
            "target": None,
            "environment": environment,
            "confidence": 0.0,
            "requires_approval": True,
            "missing_information": missing_information,
            "route": "request_more_information" if missing_information else "human_handoff",
            "reason": "no_knowledge_evidence",
            "mode": "deterministic_no_knowledge",
            "warnings": list(research_output.get("warnings") or []),
            # Present and empty, for the same reason the two shapes exist at
            # all: a consumer should be able to read the key without first
            # asking which exit produced the resolution. Empty is also the
            # correct answer here — nothing was asked, because nothing was
            # retrieved to ground an answer in.
            "runbook_candidates": [],
            "runbook_mode": "skipped_no_knowledge",
            "llm": {
                "diagnosis_polish": _llm_trace(_disabled_outcome()),
                "runbook_candidates": _llm_trace(_disabled_outcome()),
            },
            "producer": self.name,
            **_historical_block(historical, referenced=bool(historical), chosen_action=None),
        }

    def _polish_diagnosis(self, objective: str, resolution: dict) -> tuple[LlmOutcome, str | None]:
        """Optional rewording of the diagnosis. Cannot change what will run.

        Returns the outcome alongside the text so the caller can say whether the
        plain diagnosis is plain because no model was asked or because the one
        that was asked did not answer.
        """
        outcome = _expert_call(
            self.name,
            (
                "You are an enterprise IT resolution agent. Rewrite the supplied diagnosis in one or two "
                "operational sentences using only the evidence given. Return JSON with a single field "
                "`diagnosis`. Do not propose a different action, do not add facts that are not in the "
                "evidence, and do not speculate about causes the evidence does not mention."
            ),
            {
                "objective": objective,
                "deterministic_diagnosis": resolution["diagnosis"],
                "action_type": resolution["action_type"],
                "evidence": resolution["evidence"],
            },
            operation="resolution_diagnosis_polish",
        )
        text = str((outcome.value or {}).get("diagnosis") or "").strip() if outcome.ok else ""
        return outcome, (text or None)

    def _runbook_candidates(self, objective: str, resolution: dict) -> dict:
        """Scenario 2: proposed steps, which change nothing that will run.

        This platform's order is evidence → deterministic action mapping → risk
        gate → approval → tool. A suggestion has to survive every one of those
        before it can cause anything, and the one thing it may never do is
        replace the action this run already selected: ``resolution["action_type"]``
        — the only field the gate reads — is not touched here or anywhere
        downstream, and the steps below are attached with ``advisory_only``
        set so a reader cannot mistake them for a plan of record.

        Two gates are applied here, in code, before a step is even reported.
        ``ACTION_CLASSES`` membership is the deterministic action mapping: a
        name the platform does not know is dropped rather than passed along for
        someone else to interpret. And nothing is asked at all when this
        resolution is ``NO_KNOWLEDGE`` — that path means retrieval found no
        policy to stand on, and a model asked to fill that silence would be
        inventing the knowledge the platform just said it did not have.

        The case where these do carry information is the fallback: when
        ``_select_action`` found nothing to go on it returns read-only
        diagnostics, and the candidates are then the only account of what a fix
        might involve. Even there they are reported, not adopted.
        """
        empty = {"steps": [], "mode": "not_attempted", "outcome": _disabled_outcome()}
        if not settings.llm_runbook_candidates_enabled:
            return empty
        if resolution.get("status") != "PROPOSED":
            return empty
        evidence = list(resolution.get("evidence") or [])
        if not evidence:
            return empty
        outcome = _expert_call(
            self.name,
            (
                "You are an enterprise IT resolution agent. Propose candidate runbook steps that are "
                "supported by the supplied evidence only. Return JSON with a `steps` array, where each "
                "step has description, action_type, requires_approval and citation. Use only action_type "
                "values from the supplied list. Never propose a step the evidence does not support, and "
                "never propose a step that deletes data. Return an empty steps array if the evidence does "
                "not support any."
            ),
            {
                "objective": objective,
                "diagnosis": resolution.get("diagnosis"),
                "selected_action_type": resolution.get("action_type"),
                "selected_action_reason": resolution.get("reason"),
                "allowed_action_types": sorted(ACTION_CLASSES),
                "evidence": evidence[:3],
            },
            operation="resolution_runbook_candidates",
            schema=RunbookCandidate,
        )
        if not outcome.ok:
            return {"steps": [], "mode": _reasoning_mode(
                outcome, deterministic="deterministic_no_candidates", augmented="llm_candidates"
            ), "outcome": outcome}
        steps = []
        for step in (outcome.value or {}).get("steps") or []:
            proposed = str(step.get("action_type") or "").strip()
            action_class = ACTION_CLASSES.get(proposed)
            if action_class is None or not action_class.allowed:
                # Deterministic action mapping: the platform's own vocabulary is
                # the filter, so an unknown name cannot travel any further — and
                # neither can a name the gate has already ruled out. The denied
                # classes (``data_delete`` and friends) are keys of
                # ``ACTION_CLASSES`` so the gate can *name* what it refused;
                # listing one here would read as an endorsement of something the
                # platform has decided never to run.
                continue
            steps.append(
                {
                    "description": str(step.get("description") or "")[:400],
                    "action_type": proposed,
                    "tool_name": action_class.tool_name,
                    "proposed_requires_approval": bool(step.get("requires_approval", True)),
                    "citation": step.get("citation"),
                    # Advisory, and labelled as such at the field level rather
                    # than only in a docstring nobody downstream will read.
                    "advisory_only": True,
                    # The deterministic requirement wins where the two disagree.
                    # A model cannot mark a step approval-free that the action
                    # class says needs one, but it may mark one as needing
                    # approval that the class does not require.
                    "requires_approval": bool(
                        action_class.requires_approval or step.get("requires_approval", True)
                    ),
                }
            )
        return {"steps": steps[:5], "mode": "llm_candidates_advisory", "outcome": outcome}


def _resolution_evidence(research_output: dict) -> list[dict]:
    """Keep only evidence items that actually carry a quotable snippet."""
    evidence: list[dict] = []
    for item in research_output.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        snippet = str(item.get("snippet") or "").strip()
        if not snippet:
            continue
        evidence.append(
            {
                "source": str(item.get("source") or "unknown"),
                "title": str(item.get("title") or "Policy evidence"),
                "snippet": snippet,
                "score": item.get("score"),
                "article_id": item.get("article_id"),
                "document_id": item.get("document_id"),
                "chunk_id": item.get("chunk_id"),
            }
        )
    return evidence


def _sufficient_evidence(research_output: dict, evidence: list[dict]) -> bool:
    """Whether retrieval produced enough to justify proposing an action.

    Both signals are produced by ``RagResearchAgent`` already; nothing new is
    invented here. ``no_relevant_policy_evidence`` is set when the merged
    channels came back empty, and ``evidence_count`` is the merged length.
    """
    if len(evidence) < MIN_EVIDENCE_ITEMS:
        return False
    if "no_relevant_policy_evidence" in (research_output.get("warnings") or []):
        return False
    return int(research_output.get("evidence_count") or 0) >= MIN_EVIDENCE_ITEMS


def _historical_evidence(historical_output: dict) -> list[dict]:
    """Normalise the historical channel's results into evidence-shaped items.

    Shaped like ``_resolution_evidence`` so a reader can compare the two lists
    side by side, but deliberately *not* merged into it and never returned under
    ``evidence``. The ``source`` is the historical index rather than an article
    source, which is what makes the two channels tellable apart downstream.
    """
    evidence: list[dict] = []
    for item in (historical_output or {}).get("results") or []:
        if not isinstance(item, dict):
            continue
        ticket_id = str(item.get("ticket_id") or "").strip()
        snippet = str(item.get("resolution") or "").strip()
        if not ticket_id or not snippet:
            continue
        evidence.append(
            {
                "source": str(item.get("source") or "historical_ticket_index"),
                "ticket_id": ticket_id,
                "title": str(item.get("title") or "Historical ticket"),
                "snippet": snippet,
                "resolution_action": _first_text(item.get("resolution_action")),
                "category": _first_text(item.get("category")),
                "environment": _first_text(item.get("environment")),
                "status": _first_text(item.get("status")),
                "score": item.get("score"),
                "similarity": item.get("similarity"),
            }
        )
    return evidence


def _historical_block(
    historical: list[dict], *, referenced: bool, chosen_action: str | None
) -> dict:
    """The four ``historical_*`` keys, built in one place for both exit paths.

    ``referenced`` is the §七 flag and it is the caller's to set, because only
    the caller knows which of the two exits it is on: it means "the history is
    standing in for the missing policy", which is true exactly when knowledge
    was insufficient. ``historical_count`` is reported either way, so "we looked
    at history and it agreed" stays distinguishable from "we never looked".

    ``historical_divergence`` compares the *top-ranked* past handling against the
    action this run chose. Comparing against every retrieved ticket instead
    would set the flag on nearly every multi-result query — three precedents
    rarely agree — and a warning that is always on carries no information. When
    the best match records no action, the next-ranked one that does is used.
    """
    precedent = next(
        (item for item in historical if item.get("resolution_action")), None
    )
    divergence = bool(
        chosen_action
        and precedent
        and precedent["resolution_action"] != chosen_action
    )
    if referenced:
        note = (
            "历史工单仅供参考，不是正式政策，不能作为执行依据；"
            "缺少可用知识，本单需人工处理。"
        )
    elif historical:
        note = "历史工单仅供参考，不作为政策依据；本次动作由正式知识决定。"
    else:
        note = "未检索到相似历史工单；本次动作由正式知识决定。"
    return {
        "historical_evidence": historical,
        "historical_count": len(historical),
        "historical_reference": bool(referenced),
        "historical_divergence": divergence,
        "historical_note": note,
    }


def _select_action(
    objective: str, intent: str, entities: dict, environment: str | None, evidence: list[dict]
) -> tuple[str, str]:
    """Deterministic action selection. The ordering is the specification.

    An explicit cue in the request beats an inference; an inference beats a
    guess; and when nothing points anywhere the fallback is read-only
    diagnostics, which is always safe to run and never over-reaches.
    """
    if intent == "PERMISSION_REQUEST":
        return "permission_grant", "intent_permission_request"
    if intent in {"ASSET_REQUEST", "SOFTWARE_REQUEST"}:
        return "no_action", f"intent_{intent.lower()}_routed_to_team"

    lowered = str(objective or "").lower()
    if _contains_any(lowered, RESTART_KEYWORDS):
        return "service_restart", "request_reports_service_down"
    if _contains_any(lowered, CACHE_KEYWORDS):
        return "cache_flush", "request_reports_cache_pressure"

    service = str(entities.get("service") or "").upper()
    if environment == "production" and service in CRITICAL_SERVICES:
        return "service_restart", f"production_incident_on_critical_service:{service.lower()}"

    evidence_text = " ".join(str(item.get("snippet") or "") for item in evidence).lower()
    if _contains_any(evidence_text, RESTART_KEYWORDS):
        return "service_restart", "evidence_recommends_restart"
    if _contains_any(evidence_text, CACHE_KEYWORDS):
        return "cache_flush", "evidence_recommends_cache_flush"

    return INCIDENT_DEFAULT_ACTION, "no_actionable_evidence_defaulting_to_read_only"


def _action_arguments(action_type: str, ticket: dict, entities: dict, triage: dict) -> dict:
    """Build the tool kwargs for the selected action.

    The tool is not called here; these arguments are handed to the risk gate
    and, if it clears them, to ``call_tool``.
    """
    asset_id = _first_text(ticket.get("asset_id"))
    if action_type == "permission_grant":
        return {
            "employee_id": _first_text(ticket.get("requester_user_id")),
            "resource": _first_text(entities.get("resource"), entities.get("service"), "GENERAL"),
            "access_level": _first_text(entities.get("access_level"), "read_only"),
        }
    if action_type == "no_action":
        return {}
    return {"asset_id": asset_id}


def _diagnosis(evidence: list[dict]) -> str:
    """Compose the diagnosis from the retrieved evidence, quoting it directly.

    Deliberately a quotation rather than a paraphrase: a reader can check it
    against the cited article, and the agent cannot assert anything the
    evidence did not say.
    """
    primary = evidence[0]
    snippet = str(primary.get("snippet") or "").strip()
    return f"依据《{primary.get('title')}》（来源：{primary.get('source')}）：{snippet[:200]}"


def _resolution_confidence(evidence: list[dict]) -> float:
    """Evidence-strength score, reported for audit only. Never read by the gate."""
    return round(min(0.95, 0.5 + 0.1 * len(evidence)), 2)


def _first_text(*values) -> str | None:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return None


def _contains_any(lowered: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in lowered for keyword in keywords)


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
        suggestion = _expert_call(
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
            operation="compliance_risk_vote",
            schema=RiskVoteSuggestion,
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
        suggestion = _expert_call(
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
            operation="operational_risk_vote",
            schema=RiskVoteSuggestion,
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
        it_action: dict | None = None,
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
            it_action=it_action,
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

    def run(
        self,
        workflow_run_id: str,
        supervisor_output: dict,
        risk_output: dict | None,
        *,
        it_context: dict | None = None,
    ) -> dict:
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
            # An IT run retrieves evidence in the graph's own research nodes, so
            # ``query_enterprise_rag`` can never appear among this workflow's
            # steps and the check below would dock 25 points from every correct
            # IT run — enough to fail the gate outright. Whether the evidence was
            # actually consumed is still checked, via the research handoff below.
            if it_context is None and "query_enterprise_rag" not in tools:
                findings.append({"severity": "high", "code": "missing_rag_tool", "message": "RAG tool was not called."})
                score -= 25
            waiting_for_approval = bool(supervisor_output["plan"].get("needs_approval")) and workflow.get("status") == "waiting_approval"
            if it_context is not None:
                # An IT run works on the ticket the intake path already created,
                # so "no ticket was created" is the expected shape rather than a
                # defect. What is worth checking instead is whether the run did
                # what the deterministic gate decided — see _it_critic_penalty.
                score -= _it_critic_penalty(findings, it_context, tools, waiting_for_approval)
            elif "create_ticket" not in tools and not waiting_for_approval and not approval_denied:
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
        review = _expert_call(
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
            operation="critic_review",
            schema=CriticReviewResult,
        )
        llm_findings = _valid_critic_findings(
            (review.value or {}).get("findings") if review.ok else None
        )
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
            "reasoning_mode": _reasoning_mode(
                review, deterministic="deterministic_quality_gate", augmented="llm_augmented"
            ),
            "llm": _llm_trace(review),
        }


def _it_critic_penalty(
    findings: list[dict], it_context: dict, tools: list, waiting_for_approval: bool
) -> int:
    """Quality checks specific to an IT run. Appends findings, returns the penalty.

    These replace the ``create_ticket`` check rather than adding to it, and each
    one states a way the risk gate could have been bypassed — which is the only
    failure that really matters here. The gate's own decision is the reference
    point: a run that reached a different outcome than the gate decided is
    defective no matter how it looks step by step.
    """
    penalty = 0
    decision = it_context.get("decision")
    tool_name = it_context.get("tool_name")
    executed = bool(it_context.get("executed"))

    if "update_ticket" not in tools:
        findings.append(
            {
                "severity": "high",
                "code": "missing_ticket_update",
                "message": "The IT ticket was never updated with the outcome of the run.",
            }
        )
        penalty += 25
    if decision == "require_approval" and "request_approval" not in tools:
        findings.append(
            {
                "severity": "high",
                "code": "it_gate_bypassed",
                "message": "The risk gate required human approval but no approval was requested.",
            }
        )
        penalty += 40
    if decision == "auto_execute" and tool_name not in tools and not executed and not waiting_for_approval:
        findings.append(
            {
                "severity": "critical",
                "code": "it_action_missing",
                "message": "The risk gate cleared an automatic action but the action never ran.",
            }
        )
        penalty += 40
    if decision == "deny" and executed:
        findings.append(
            {
                "severity": "critical",
                "code": "it_denied_action_executed",
                "message": "An action the risk gate denied was executed anyway.",
            }
        )
        penalty += 40
    return penalty


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


def _expert_call(
    agent_name: str,
    system_prompt: str,
    payload: dict,
    *,
    operation: str,
    schema: type | None = None,
    max_tokens: int = 600,
) -> LlmOutcome:
    """Ask an expert agent's model a question and describe what came back.

    This replaces ``_expert_json``, which returned ``None`` for three unrelated
    situations — the LLM switched off, the call failing, and the call answering
    something unreadable. Callers could only write ``if result is None``, so a
    run that never contacted a model and a run whose model timed out produced
    byte-identical agent payloads. The outcome object keeps them apart, and
    every caller now reports which one it was (see :func:`_reasoning_mode`).

    It never raises. A model is an enhancement to these agents, and an
    enhancement that can take down the workflow it enhances is not one.

    ``schema`` is passed through to :func:`app.services.llm.call_json`, which
    validates against it and reports a validation failure as ``invalid_output``
    rather than letting a malformed field reach a business decision.
    """
    if not settings.llm_multi_agent_reasoning_enabled or not llm_ready():
        return LlmOutcome(
            status="disabled",
            operation=operation,
            provider=settings.llm_provider,
            model=settings.llm_model,
        )
    outcome = call_json(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        operation=operation,
        schema=schema,
        temperature=0,
        max_tokens=max_tokens,
    )
    record_llm_call(outcome)
    if outcome.failed:
        logger.warning(
            "multi_agent.expert_reasoning_failed",
            extra={
                "event": "multi_agent.expert_reasoning_failed",
                "agent": agent_name,
                "operation": operation,
                "status": outcome.status,
                "error_type": outcome.error_type,
                "error": outcome.error_message,
                "latency_ms": outcome.latency_ms,
                "retry_count": outcome.retry_count,
            },
        )
    return outcome


def _disabled_outcome(operation: str = "not_attempted") -> LlmOutcome:
    """The outcome for a call that was never made, for reporting symmetry.

    A path that deliberately skips the model still has to return *something*
    shaped like a call record, or the payload's ``llm`` block would be missing
    exactly where a reader most wants to know why nothing happened.
    """
    return LlmOutcome(
        status="disabled",
        operation=operation,
        provider=settings.llm_provider,
        model=settings.llm_model,
    )


def _reasoning_mode(outcome: LlmOutcome, *, deterministic: str, augmented: str) -> str:
    """Name the path that actually produced this agent's answer.

    Three states rather than two, because the old two-state version called a
    *failed* call ``deterministic_...`` — which reads as "no model was
    involved". One was; it just did not answer. Somebody debugging a quality
    drop needs to tell "the LLM is off" apart from "the LLM is broken", and
    that distinction is exactly what the section 十一 telemetry is for.
    """
    if outcome.ok:
        return augmented
    if outcome.failed:
        return f"{deterministic}_llm_failed"
    return deterministic


def _llm_trace(outcome: LlmOutcome) -> dict:
    """The model call recorded inside the agent payload, in one fixed shape.

    Identical across all five call sites on purpose: a reader comparing the
    compliance agent's payload with the critic's should not have to learn two
    vocabularies. Token counts are reported with ``available`` beside them so a
    provider that returns no usage is not silently read as having cost nothing.
    """
    return {
        "status": outcome.status,
        "operation": outcome.operation,
        "provider": outcome.provider,
        "model": outcome.model,
        "latency_ms": round(outcome.latency_ms, 2),
        "retry_count": outcome.retry_count,
        "fallback_used": outcome.fallback_used,
        "error_type": outcome.error_type,
        "usage": {
            "available": outcome.usage_available,
            "prompt_tokens": outcome.prompt_tokens,
            "completion_tokens": outcome.completion_tokens,
            "total_tokens": outcome.total_tokens,
        },
    }


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


def _apply_expert_risk_suggestion(vote: dict, outcome: LlmOutcome) -> dict:
    """Merge an independent risk vote into the deterministic one. Escalate-only.

    Every branch here can raise risk or add a warning and none can lower
    either, which is what makes the LLM's participation in risk *advisory* in
    the strong sense rather than the polite one. ``needs_approval`` is
    ``or``-ed with the deterministic value, so a model that answers "false",
    says nothing, or answers something unreadable produces the same result: the
    requirement the code already had.

    Three things are checked in code before anything is merged, because §七
    requires the model's vocabulary to be one this platform already has:

    * ``risk_level`` must be a key of :data:`RISK_ORDER`, compared by rank
      rather than by string;
    * ``needs_approval`` arrives already narrowed to ``bool | None`` by
      :class:`RiskVoteSuggestion`, which refuses to read an unrecognised value
      as "no";
    * ``warnings`` are strings and are not interpreted.

    The reasoning mode is reported from the outcome rather than asserted, so a
    payload cannot claim ``llm_augmented`` for a call that failed.
    """
    result = dict(vote)
    result["reasoning_mode"] = _reasoning_mode(
        outcome,
        deterministic="deterministic_policy",
        augmented="llm_augmented",
    )
    result["llm"] = _llm_trace(outcome)
    if not outcome.ok:
        return result
    suggestion = outcome.value or {}
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
