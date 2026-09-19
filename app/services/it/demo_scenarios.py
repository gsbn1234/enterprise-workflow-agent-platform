"""Phase 6 showcase metadata, projected from the committed evaluation suite.

The showcase needs a list of runnable IT scenarios plus a description of what
the platform can do. Neither is written down a second time here. The scenarios
are read out of ``sample_data/eval/it_incident_eval.jsonl`` -- the same file
``scripts/evaluate.py --suite it`` scores -- so ``expected_risk`` in the UI and
``expected_risk`` in the harness are one value, not two that can drift apart.
The smoke suite contributes only its ids, so a scenario can be badged as one of
the nine cases CI already runs.

The only field computed rather than copied is ``backend_entry``, which is the
same call sequence for every scenario and is derived from the expectations.

Nothing in this module decides anything, calls anything, or imports the
application. It is a reader, in the same spirit as ``app.services.demo``.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
EVAL_SUITE_PATH = ROOT / "sample_data" / "eval" / "it_incident_eval.jsonl"
SMOKE_SUITE_PATH = ROOT / "sample_data" / "eval" / "it_incident_eval_smoke.jsonl"

# The real request sequence, in order. Every scenario runs the same chain; the
# approval legs only appear when the gate is expected to stop at a human.
_CHAIN_ENTRY = [
    "POST /api/auth/login",
    "POST /api/it/requests",
    "POST /api/it/requests/{ticket_id}/resolve",
    "GET /api/it/requests/{ticket_id}/chain",
]
_APPROVAL_ENTRY = [
    "GET /api/approvals?status=pending",
    "POST /api/approvals/{approval_id}/decide",
    "GET /api/it/requests/{ticket_id}/chain",
]


#: What the platform does, grouped the way the showcase presents it. ``backend``
#: names the real entry points; ``module`` names the real implementation. Both
#: are pointers, not copies -- the UI links a reader to the code that runs.
DEMO_CAPABILITIES: list[dict[str, Any]] = [
    {
        "id": "triage",
        "title": "① 智能受理与分流",
        "what": "把一句自然语言报障变成结构化意图：intent / category / priority / entities / needs_approval。",
        "how": "确定性关键词规则，按子句证据加权。同一个句子永远得到同一个分类，没有模型参与。",
        "backend": ["GET /api/it/triage/preview", "POST /api/it/requests"],
        "module": "app/services/it/triage.py · classify()",
        "mocked": False,
        "shown_as": "chain.triage",
    },
    {
        "id": "retrieval",
        "title": "② 知识检索 / 历史工单检索",
        "what": "两条互相独立的检索通道：正式知识库（政策 / Runbook）与历史工单。",
        "how": "本地策略库，确定性关键词打分 —— 不是向量 RAG。历史工单按相似度返回，只作参考，不作依据。",
        "backend": ["GET /api/knowledge/search", "GET /api/it/requests/{ticket_id}/chain"],
        "module": "app/services/tools/knowledge.py · search_knowledge() / app/services/it/history.py · search_historical_tickets()",
        "mocked": False,
        "shown_as": "chain.resolution.evidence / chain.resolution.historical_evidence",
    },
    {
        "id": "risk_gate",
        "title": "③ Risk Gate 风险决策",
        "what": "决定一个动作是自动执行、必须人工审批、还是直接拒绝，并说明是哪一条规则下的判断。",
        "how": "纯函数，无 I/O、无 LLM。规则顺序即规范，第一条命中的规则成为 rule_id。环境与资产等级取自目标资产行，不取自用户措辞。",
        "backend": ["GET /api/it/requests/{ticket_id}/chain"],
        "module": "app/services/it/risk_gate.py · evaluate()",
        "mocked": False,
        "shown_as": "chain.risk_decision / audit 中的 it.risk_gate_decided",
    },
    {
        "id": "approval",
        "title": "④ Human Approval 人工审批",
        "what": "高风险动作在真正执行前停下来等人。审批通过才能解锁执行，拒绝则记录拒绝并且不执行。",
        "how": "LangGraph 在 human_approval 节点中断并把 run 存为 checkpoint；审批决定通过 resume 继续同一条 run。",
        "backend": ["GET /api/approvals", "POST /api/approvals/{approval_id}/decide"],
        "module": "app/services/tools/approvals.py",
        "mocked": False,
        "shown_as": "chain.approval",
    },
    {
        "id": "tool_execution",
        "title": "⑤ Mock Tool 工具执行",
        "what": "被批准的动作在这里执行：diagnose_service / flush_cache / restart_service / grant_permission。",
        "how": "全部是 mock 工具。调用被记入审计，但没有任何真实基础设施被改动。",
        "backend": ["GET /api/it/requests/{ticket_id}/chain"],
        "module": "app/services/it/actions.py · app/services/it/execution.py",
        "mocked": True,
        "shown_as": "chain.execution / audit 中的 it.action_executed",
    },
    {
        "id": "audit",
        "title": "⑥ Audit / Observability 审计与可观测",
        "what": "每一个决策都留痕：谁、在什么时候、依据什么输入、得到了什么结论。",
        "how": "审计行带前序哈希形成链，且只追加。IT 链路视图按插入顺序返回该工单的全部 it.* 事件。",
        "backend": ["GET /api/audit-logs", "GET /api/metrics/summary"],
        "module": "app/services/audit.py",
        "mocked": False,
        "shown_as": "chain.audit",
    },
    {
        "id": "evaluation",
        "title": "⑦ Evaluation 评测结果",
        "what": "21 条人工策划的 IT 案例，覆盖分流、检索、决议、风险、审批五个环节的准确率。",
        "how": "离线 harness，跑在独立的评测库上。下面的数字是历史结果，不是当前进程实时算出来的。",
        "backend": ["GET /api/eval-reports"],
        "module": "scripts/evaluate.py --suite it",
        "mocked": False,
        "shown_as": "docs/EVALUATION.md 与 data/eval_reports/*.json",
    },
]


#: The capabilities worth pointing an interviewer at, with where each one lives.
DEMO_HIGHLIGHTS: list[dict[str, str]] = [
    {"title": "LangGraph 21 节点执行链", "detail": "StateGraph 编排，检索 / 风险 / 审批 / 批判 / 自纠 / 记忆各成一个节点。", "where": "app/services/multi_agent/durable_executor.py:285"},
    {"title": "Triage 确定性分流", "detail": "关键词证据按子句加权，不依赖模型，可单测。", "where": "app/services/it/triage.py:289"},
    {"title": "Knowledge Retrieval", "detail": "本地策略库检索，返回 article_id / title / score / snippet 与出处。", "where": "app/services/tools/knowledge.py:156"},
    {"title": "Historical Ticket Retrieval", "detail": "独立的历史工单通道，与正式知识分开计分、分开标记。", "where": "app/services/it/history.py:46"},
    {"title": "Risk Gate 确定性门禁", "detail": "纯函数，三种结论，规则顺序即规范，环境与等级取自资产行。", "where": "app/services/it/risk_gate.py:210"},
    {"title": "RBAC 分级授权", "detail": "employee < manager < it_support < it_admin < admin，工具级 fail-closed。", "where": "app/services/it/rbac.py:86"},
    {"title": "Human-in-the-loop 审批", "detail": "高风险动作在 checkpoint 处中断，等人决定后 resume 同一条 run。", "where": "app/services/multi_agent/durable_executor.py:1207"},
    {"title": "Mock Tool Execution", "detail": "变更类工具全部是 mock，调用被审计但不触碰真实基础设施。", "where": "app/services/it/actions.py"},
    {"title": "Audit Trail", "detail": "哈希链式审计，每个决策记录其输入与规则。", "where": "app/services/audit.py"},
    {"title": "Checkpoint / Resume", "detail": "run 状态可持久化并按节点恢复，审批前后是同一条 run。", "where": "app/services/multi_agent/durable_executor.py"},
]


#: The evaluation numbers the showcase is allowed to show, and the terms it is
#: allowed to show them under.
#:
#: These are a *record of a past run*, transcribed from the committed
#: ``docs/EVALUATION.md`` -- not a live measurement. ``data/eval_reports/`` is
#: gitignored, so a fresh clone has no artifacts and ``/api/eval-reports``
#: returns an empty list; this block is what the UI falls back to, under a label
#: that says so. Nothing here is recomputed and nothing is interpolated.
EVALUATION_HISTORY: dict[str, Any] = {
    "label": "Phase 4 Evaluation Result · 历史结果，非实时",
    "recorded_at": "2026-09-18T17:20:36+00:00",
    "record_id": "eval_c345178b1227f15d",
    "source_doc": "docs/EVALUATION.md §6",
    "artifact": "data/eval_reports/phase4_summary.json",
    "artifact_note": "data/ 被 .gitignore 忽略：全新 clone 上不存在这些产物，因此本页不读取它们。",
    "reproduce": "python scripts/evaluate.py --suite it --report-prefix phase4",
    "disclaimer": (
        "以上数字来自已提交文档中记录的一次历史运行，不是当前进程实时计算的结果。"
        "若这是一个全新 clone、没有 data/eval_reports，本页不会伪造任何数字。"
    ),
    "metrics": [
        {"key": "total_cases", "label": "案例总数", "value": 21, "unit": "count"},
        {"key": "passed_failed", "label": "通过 / 失败", "value": "20 / 1", "unit": "text"},
        {"key": "pass_rate", "label": "通过率", "value": 0.9524, "unit": "percent"},
        {"key": "triage_accuracy", "label": "分流准确率", "value": 1.0, "unit": "percent"},
        {"key": "retrieval_recall_at_3", "label": "检索 Recall@3", "value": 0.9524, "unit": "percent"},
        {"key": "resolution_action_accuracy", "label": "决议动作准确率", "value": 1.0, "unit": "percent"},
        {"key": "risk_decision_accuracy", "label": "风险决策准确率", "value": 1.0, "unit": "percent"},
        {"key": "approval_accuracy", "label": "审批触发准确率", "value": 1.0, "unit": "percent"},
        {"key": "unsafe_tool_execution_count", "label": "不安全工具执行", "value": 0, "unit": "count"},
        {"key": "unexpected_auto_execution_count", "label": "本该审批却自动执行", "value": 0, "unit": "count"},
        {"key": "historical_hit_accuracy", "label": "历史命中准确率", "value": 1.0, "unit": "percent"},
        {"key": "historical_reference_accuracy", "label": "历史引用准确率", "value": 1.0, "unit": "percent"},
    ],
    "headline": "Phase 4 的核心目标是把 unexpected_auto_execution_count 从 1 降到 0；unsafe_tool_execution_count 前后都是 0（未提升，如实记录）。",
    "unchanged": "retrieval_recall_at_3 与 Phase 3 相同（0.9524），未提升。",
}


@lru_cache(maxsize=1)
def _read_eval_suite() -> tuple[list[dict[str, Any]], list[str]]:
    """Return (evaluation cases, smoke-subset ids). Cached; the files are static."""
    cases = _read_jsonl(EVAL_SUITE_PATH)
    smoke_ids = [str(case.get("id")) for case in _read_jsonl(SMOKE_SUITE_PATH)]
    return cases, smoke_ids


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _demo_class(case: dict[str, Any]) -> str:
    """Which of the three demo shapes this case demonstrates."""
    if str(case.get("expected_risk")) == "deny":
        return "risk_deny"
    if case.get("expected_approval"):
        return "approval_approved" if case.get("approve") else "approval_rejected"
    return "auto_execute"


def _backend_entry(case: dict[str, Any]) -> list[str]:
    entry = list(_CHAIN_ENTRY)
    if case.get("expected_approval"):
        entry.extend(_APPROVAL_ENTRY)
    return entry


def _project(case: dict[str, Any], smoke_ids: list[str]) -> dict[str, Any]:
    retrieval = case.get("expected_retrieval") or {}
    injected = case.get("inject_action_type")
    return {
        "id": str(case.get("id") or ""),
        "objective": case.get("objective") or "",
        "category": case.get("expected_category"),
        "intent": case.get("expected_intent"),
        "expected_action": case.get("expected_action"),
        "expected_risk": case.get("expected_risk"),
        "expected_approval": bool(case.get("expected_approval")),
        "expected_historical_reference": bool(case.get("expected_historical_reference")),
        "expect_executed": bool(case.get("expect_executed")),
        "expected_knowledge": list(retrieval.get("knowledge") or []),
        "expected_historical_min_count": int(retrieval.get("historical_min_count") or 0),
        "demo_class": _demo_class(case),
        "smoke_subset": str(case.get("id")) in smoke_ids,
        "backend_entry": _backend_entry(case),
        # A case the harness drives by injecting an action type cannot be
        # reproduced over HTTP, because the product has no such parameter. It is
        # offered as a recorded result instead of a button that would have to
        # fake the injection.
        "runnable": injected is None,
        "not_runnable_reason": (
            None
            if injected is None
            else f"该用例由评测 harness 注入 action_type={injected} 驱动；产品 API 没有这个参数，因此无法现场复现，只作历史结果展示。"
        ),
        "notes": case.get("notes") or "",
    }


def list_it_demo_scenarios() -> dict[str, Any]:
    """The showcase payload: real scenarios plus the capability index.

    Shaped like the other retrieval helpers in this codebase (``source`` /
    ``available`` / results) so a missing evaluation file reads as data rather
    than as a 500.
    """
    cases, smoke_ids = _read_eval_suite()
    scenarios = [_project(case, smoke_ids) for case in cases]
    return {
        "source": "sample_data/eval/it_incident_eval.jsonl",
        "smoke_source": "sample_data/eval/it_incident_eval_smoke.jsonl",
        "available": bool(scenarios),
        "count": len(scenarios),
        "runnable_count": sum(1 for item in scenarios if item["runnable"]),
        "smoke_count": sum(1 for item in scenarios if item["smoke_subset"]),
        "scenarios": scenarios,
        "capabilities": DEMO_CAPABILITIES,
        "highlights": DEMO_HIGHLIGHTS,
        "evaluation_history": EVALUATION_HISTORY,
    }
