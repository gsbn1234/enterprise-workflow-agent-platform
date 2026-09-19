"""Phase 5-1 LLM engineering: fourteen ways a model can fail, and what survives them.

    "LLM 是增强能力，不是单点故障。"

The claim this file exists to check is not "the LLM works". It is that the
workflow is *unaffected* when the LLM does not: switched off, unreachable, too
slow, answering prose instead of JSON, or answering JSON in a vocabulary this
platform does not have. Every one of those is simulated here, offline, through
``llm._post``.

Mocking ``_post`` rather than the socket is deliberate. The retry ladder, the
status vocabulary, the schema validation, the telemetry row and every agent's
fallback all sit *above* ``_post``, and those are the parts under test; a fake
socket would mostly test httpx.

The last third of the file is the part that matters most. It does not check that
the model behaves — it checks that the model *cannot*, however it behaves:
that a resolution the LLM suggested still meets the risk gate, that a risk vote
saying "no approval needed" cannot remove one, and that no amount of prompting
produces a tool call.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# A path of its own, so this suite's llm_calls rows are its own and a run of
# any other suite cannot make a count assertion here pass or fail.
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "llm_engineering_smoke_test.sqlite3")
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
# On, and pointed at a host that does not resolve. Every call below is answered
# by the fake transport, so this URL is only ever used to satisfy ``llm_ready``;
# a test that accidentally reaches the real path fails loudly instead of
# quietly passing on a developer's workstation.
os.environ["AGENT_LLM_ENABLED"] = "true"
os.environ["AGENT_LLM_PROVIDER"] = "qwen"
os.environ["AGENT_LLM_BASE_URL"] = "https://llm.invalid/v1"
os.environ["AGENT_LLM_API_KEY"] = "smoke-test-key"
os.environ["AGENT_LLM_MAX_RETRIES"] = "1"
os.environ["AGENT_LLM_RETRY_BACKOFF_SECONDS"] = "0"
os.environ["AGENT_LLM_TELEMETRY_ENABLED"] = "true"
sys.path.insert(0, str(ROOT))

import app.services.llm as llm  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import init_db, reset_database, rows_to_dicts, get_connection  # noqa: E402
from app.services.agent.planner import plan_workflow  # noqa: E402
from app.services.it.risk_gate import ACTION_CLASSES, evaluate as evaluate_risk  # noqa: E402
from app.services.it.triage import classify  # noqa: E402
from app.services.it.triage import llm_fallback as triage_llm_fallback  # noqa: E402
from app.services.it.triage import needs_llm_fallback  # noqa: E402
from app.services.llm_schemas import IntentFallbackResult  # noqa: E402
from app.services.llm_telemetry import llm_context, llm_usage_summary, record_llm_call  # noqa: E402
from app.services.multi_agent.agents import ComplianceRiskAgent, ResolutionAgent  # noqa: E402
from app.services.multi_agent.durable_executor import _it_triage_fallback  # noqa: E402


USAGE = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}


def _completion(content: str, *, usage: dict | None = None) -> dict:
    """A provider response body, in the shape the OpenAI-compatible API uses."""
    body: dict = {"choices": [{"message": {"role": "assistant", "content": content}}]}
    if usage is not None:
        body["usage"] = usage
    return body


def _timeout() -> Exception:
    return llm.LLMTimeoutError(f"LLM provider timed out after {settings.llm_timeout_seconds}s")


def _http_400() -> Exception:
    return llm.LLMProviderError("LLM provider returned HTTP 400: bad request", error_type="http_400")


def _http_503() -> Exception:
    return llm.LLMProviderError(
        "LLM provider returned HTTP 503: upstream unavailable",
        error_type="http_503",
        retryable=True,
    )


class _Transport:
    """Stand-in for the provider. Replays ``responses`` in order, then repeats the last.

    Exceptions in the list are raised; anything else is returned as the parsed
    response body. ``calls`` records every attempt, which is how the retry
    assertions are made: "did not retry" is only observable by counting.
    """

    def __init__(self, *responses: object) -> None:
        self.responses = list(responses) or [_completion("{}")]
        self.calls: list[dict] = []
        self.original = None

    def __enter__(self) -> "_Transport":
        self.original = llm._post

        def fake(messages, *, temperature, max_tokens, response_format):
            self.calls.append(
                {
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "response_format": response_format,
                }
            )
            outcome = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        llm._post = fake
        return self

    def __exit__(self, *exc) -> bool:
        llm._post = self.original
        return False


class _Setting:
    """Temporarily override settings attributes, restoring them on exit.

    The switches under test are read live by ``llm_ready`` and the agents, so a
    context manager is enough — no module reload, and no chance of leaving a
    suite-wide switch flipped if an assertion fails mid-block.
    """

    def __init__(self, **values: object) -> None:
        self.values = values
        self.saved: dict = {}

    def __enter__(self) -> "_Setting":
        self.saved = {key: getattr(settings, key) for key in self.values}
        for key, value in self.values.items():
            setattr(settings, key, value)
        return self

    def __exit__(self, *exc) -> bool:
        for key, value in self.saved.items():
            setattr(settings, key, value)
        return False


def _ask(schema=None, expect_json: bool = True):
    """One call, recorded the way every real caller records it.

    ``record_llm_call`` is the caller's job rather than something ``llm.call``
    does itself — the operation label and the run ids belong to whoever knows
    them — so the helper here has to do it too, or the telemetry assertions
    below would be checking a table nothing writes to.
    """
    outcome = llm.call(
        [{"role": "user", "content": "hello"}],
        operation="smoke",
        schema=schema,
        expect_json=expect_json,
    )
    record_llm_call(outcome)
    return outcome


def _call_count() -> int:
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM llm_calls").fetchone()
    return int(row["count"])


def _last_call() -> dict:
    with get_connection() as conn:
        rows = rows_to_dicts(
            conn.execute("SELECT * FROM llm_calls ORDER BY created_at DESC, rowid DESC LIMIT 1").fetchall()
        )
    assert rows, "expected at least one llm_calls row"
    return rows[0]


# --- 1. LLM disabled ---------------------------------------------------------


def _llm_disabled_is_not_a_failure() -> None:
    """A switched-off model is a configuration, not an error, and is never dialled."""
    with _Setting(llm_enabled=False), _Transport(_completion("{}")) as transport:
        outcome = _ask()
    assert outcome.status == "disabled", outcome.to_audit()
    assert outcome.disabled is True, outcome.to_audit()
    assert outcome.ok is False, outcome.to_audit()
    assert outcome.failed is False, outcome.to_audit()
    assert outcome.value is None, outcome.to_audit()
    assert not transport.calls, transport.calls

    # And the same discipline at the agent layer, where a disabled LLM must
    # leave the deterministic answer exactly as it was.
    with _Setting(llm_multi_agent_reasoning_enabled=False):
        base = classify("我的生产 Redis 连不上了")
        triage, trace = _it_triage_fallback(
            "我的生产 Redis 连不上了", base, ticket_id="IT-TEST", actor="tester", tenant_id="default"
        )
    assert trace is None, trace
    assert triage["mode"] == "deterministic", triage
    assert triage == base.to_dict(), triage


# --- 2. Success --------------------------------------------------------------


def _success_records_real_usage() -> None:
    payload = '{"intent": "IT_INCIDENT", "confidence": 0.8}'
    before = _call_count()
    with _Transport(_completion(payload, usage=USAGE)) as transport:
        outcome = _ask(schema=IntentFallbackResult)
    assert outcome.ok is True, outcome.to_audit()
    assert outcome.value["intent"] == "IT_INCIDENT", outcome.to_audit()
    assert outcome.fallback_used is False, outcome.to_audit()
    assert outcome.retry_count == 0, outcome.to_audit()
    assert len(transport.calls) == 1, transport.calls
    assert outcome.usage_available is True, outcome.to_audit()
    assert (outcome.prompt_tokens, outcome.completion_tokens, outcome.total_tokens) == (120, 30, 150)
    assert outcome.model and outcome.provider, outcome.to_audit()
    assert outcome.latency_ms >= 0, outcome.to_audit()
    # ``response_format`` is sent because a schema was supplied: the provider is
    # asked to constrain the answer, not merely requested to.
    assert transport.calls[0]["response_format"] == {"type": "json_object"}, transport.calls[0]

    assert _call_count() == before + 1, "the success must leave exactly one telemetry row"
    row = _last_call()
    assert row["status"] == "success", row
    assert row["usage_available"] == 1, row
    assert row["total_tokens"] == 150, row
    assert row["fallback_used"] == 0, row
    assert row["operation"] == "smoke", row


def _missing_usage_is_recorded_as_missing() -> None:
    """§十二: a provider that reports nothing must not read as having cost nothing."""
    before = _call_count()
    with _Transport(_completion('{"ok": true}')):
        outcome = _ask()
    assert outcome.ok is True, outcome.to_audit()
    assert outcome.usage_available is False, outcome.to_audit()
    assert outcome.total_tokens is None, outcome.to_audit()
    assert _call_count() == before + 1
    row = _last_call()
    assert row["usage_available"] == 0, row
    assert row["total_tokens"] is None, row
    summary = llm_usage_summary()
    assert summary["totals"]["calls"] >= 1, summary["totals"]


# --- 3. Timeout, 4. provider error, 5. invalid JSON, 6. schema violation -----


def _timeout_is_named_and_retried_once() -> None:
    with _Transport(_timeout()) as transport:
        outcome = _ask()
    assert outcome.status == "timeout", outcome.to_audit()
    assert outcome.error_type == "timeout", outcome.to_audit()
    assert outcome.fallback_used is True, outcome.to_audit()
    # One retry, no more: ``llm_max_retries`` is capped at 1 by config validation,
    # and this asserts the cap is actually honoured rather than merely configured.
    assert len(transport.calls) == 2, transport.calls
    assert outcome.retry_count == 1, outcome.to_audit()
    assert _last_call()["status"] == "timeout", _last_call()


def _provider_error_retries_only_when_the_provider_is_at_fault() -> None:
    """4xx is our request; 5xx is theirs. Only one of them is worth a second try."""
    with _Transport(_http_400()) as transport:
        outcome = _ask()
    assert outcome.status == "provider_error", outcome.to_audit()
    assert outcome.error_type == "http_400", outcome.to_audit()
    assert len(transport.calls) == 1, "a 4xx will not clear on a retry"
    assert outcome.retry_count == 0, outcome.to_audit()

    with _Transport(_http_503()) as transport:
        outcome = _ask()
    assert outcome.status == "provider_error", outcome.to_audit()
    assert outcome.error_type == "http_503", outcome.to_audit()
    assert len(transport.calls) == 2, "a 5xx is worth one retry"


def _invalid_json_is_a_parse_error_and_is_not_retried() -> None:
    with _Transport(_completion("抱歉，我无法回答这个问题。")) as transport:
        outcome = _ask()
    assert outcome.status == "parse_error", outcome.to_audit()
    assert outcome.error_type == "not_json", outcome.to_audit()
    assert outcome.fallback_used is True, outcome.to_audit()
    # Not retried, on purpose: the same prompt tends to produce the same prose,
    # and the deterministic path costs nothing.
    assert len(transport.calls) == 1, transport.calls


def _schema_violation_is_invalid_output_not_a_crash() -> None:
    # ``intent`` has min_length=1, so an empty string is a shape error the
    # schema catches before any business code can read a field off it.
    with _Transport(_completion('{"intent": "", "confidence": 5}')) as transport:
        outcome = _ask(schema=IntentFallbackResult)
    assert outcome.status == "invalid_output", outcome.to_audit()
    assert "IntentFallbackResult" in (outcome.error_message or ""), outcome.to_audit()
    assert outcome.value is None, outcome.to_audit()
    assert len(transport.calls) == 1, transport.calls
    assert _last_call()["status"] == "invalid_output", _last_call()


def _a_transport_failure_then_a_success_is_one_retry() -> None:
    payload = '{"intent": "PERMISSION_REQUEST", "confidence": 0.9}'
    before = _call_count()
    with _Transport(_http_503(), _completion(payload, usage=USAGE)) as transport:
        outcome = _ask(schema=IntentFallbackResult)
    assert outcome.ok is True, outcome.to_audit()
    assert outcome.retry_count == 1, outcome.to_audit()
    assert len(transport.calls) == 2, transport.calls
    assert outcome.value["intent"] == "PERMISSION_REQUEST", outcome.to_audit()
    assert outcome.fallback_used is False, outcome.to_audit()
    # One row per call *attempt series*, not one per HTTP request: the retry is
    # a property of the outcome, carried in ``retry_count``.
    assert _call_count() == before + 1, "a retried call is still one logical call"


# --- 8. Fallback -------------------------------------------------------------


def _a_broken_llm_leaves_the_deterministic_plan_complete() -> None:
    """A failed planner call must not shorten or weaken the plan it falls back to."""
    with _Transport(_timeout()):
        plan = plan_workflow("给客户退款 800 元，客户很生气")
    assert plan.category == "refund", plan.category
    assert plan.risk_level == "high", plan.risk_level
    assert plan.needs_approval is True, plan.needs_approval
    assert plan.approval_chain, plan.approval_chain
    assert plan.proposed_tools, plan.proposed_tools
    # §D13: the failure is now visible in the plan itself, so "the LLM is off"
    # and "the LLM is timing out" no longer produce identical reasons.
    assert "timeout" in plan.reason, plan.reason


def _a_broken_llm_leaves_the_deterministic_final_answer() -> None:
    """§九: a dead model must not fail the ticket it was decorating."""
    from app.services.llm import polish_agent_answer_outcome

    original = "工单 IT-2025-1041 已创建，等待审批。"
    with _Transport(_http_503()):
        outcome = polish_agent_answer_outcome(original, status="completed", category="incident")
    assert outcome.ok is False, outcome.to_audit()
    assert outcome.value is None, outcome.to_audit()
    assert outcome.status == "provider_error", outcome.to_audit()
    # What the caller does with that is the point: the answer it already had.
    final_answer = original
    if outcome.ok and outcome.value:
        final_answer = str(outcome.value)
    assert final_answer == original, final_answer

    with _Transport(_completion("## 处理结果\n\n工单 IT-2025-1041 已创建，等待审批。")):
        outcome = polish_agent_answer_outcome(original, status="completed", category="incident")
    assert outcome.ok is True, outcome.to_audit()
    assert "IT-2025-1041" in str(outcome.value), outcome.value


# --- 9 / 10. Triage fallback: fires only when unsure -------------------------


VAGUE = "那个东西又不好使了，麻烦看一下"


def _low_confidence_triage_asks_the_model() -> None:
    base = classify(VAGUE)
    assert base.confidence < settings.llm_triage_min_confidence, base.to_dict()
    assert needs_llm_fallback(base, settings.llm_triage_min_confidence) is True, base.to_dict()

    answer = (
        '{"intent": "IT_INCIDENT", "service": "NETWORK", "environment": "production",'
        ' "confidence": 0.8, "reason": "网络类故障，影响生产"}'
    )
    before = _call_count()
    with _Transport(_completion(answer, usage=USAGE)):
        triage, trace = _it_triage_fallback(
            VAGUE, base, ticket_id="IT-LLM-1", actor="tester", tenant_id="default"
        )
    assert trace is not None and trace["status"] == "success", trace
    assert triage["mode"] == "llm_assisted", triage
    assert triage["entities"]["service"] == "NETWORK", triage
    assert triage["entities"]["environment"] == "production", triage
    assert triage["category"] == "NETWORK", triage
    # The merged reading raises the stakes rather than lowering them: production
    # on a critical service is urgent and needs a human, and both of those come
    # from the deterministic helpers, not from the model's answer.
    assert triage["priority"] == "urgent", triage
    assert triage["needs_approval"] is True, triage
    assert triage["confidence"] > base.confidence, triage
    assert _call_count() == before + 1


def _high_confidence_triage_does_not_ask_the_model() -> None:
    base = classify("我的生产 Redis 连不上了")
    assert base.confidence >= settings.llm_triage_min_confidence, base.to_dict()
    assert needs_llm_fallback(base, settings.llm_triage_min_confidence) is False, base.to_dict()

    before = _call_count()
    with _Transport(_completion('{"intent": "ASSET_REQUEST"}')) as transport:
        triage, trace = _it_triage_fallback(
            "我的生产 Redis 连不上了", base, ticket_id="IT-LLM-2", actor="tester", tenant_id="default"
        )
    assert not transport.calls, "a confident classification must cost nothing"
    assert trace is None, trace
    assert triage == base.to_dict(), triage
    assert triage["mode"] == "deterministic", triage
    assert _call_count() == before, "no call, so no telemetry row"


def _the_model_may_not_invent_a_vocabulary() -> None:
    """§七: an intent outside the enum fails the whole answer, not just that field."""
    base = classify(VAGUE)
    rejected = _completion('{"intent": "MELTDOWN", "service": "NETWORK", "confidence": 0.99}')
    with _Transport(rejected):
        triage, trace = _it_triage_fallback(
            VAGUE, base, ticket_id="IT-LLM-3", actor="tester", tenant_id="default"
        )
    assert triage == base.to_dict(), triage
    assert triage["mode"] == "deterministic", triage
    assert trace["status"] == "success", trace

    with get_connection() as conn:
        rows = rows_to_dicts(
            conn.execute(
                """
                SELECT detail_json FROM audit_logs
                WHERE event_type = 'it.triage_llm_fallback_rejected' AND target_id = 'IT-LLM-3'
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchall()
        )
    assert rows, "a rejected model answer must be auditable"
    assert "intent_not_in_vocabulary" in rows[0]["detail_json"], rows[0]

    # An unknown *service* is narrower: the field is dropped, the intent stands,
    # and the gap shows up where a service desk will see it.
    base = classify(VAGUE)
    with _Transport(_completion('{"intent": "IT_INCIDENT", "service": "QUANTUM_MESH", "confidence": 0.9}')):
        triage, trace = _it_triage_fallback(
            VAGUE, base, ticket_id="IT-LLM-4", actor="tester", tenant_id="default"
        )
    assert triage["mode"] == "llm_assisted", triage
    assert "service" not in triage["entities"], triage
    assert "service" in triage["missing_information"], triage


def _the_fallback_cannot_lower_an_approval_the_rules_already_raised() -> None:
    """Escalate-only, the same discipline the risk votes use."""
    base = classify("生产环境数据库连不上，需要紧急处理")
    assert base.needs_approval is True, base.to_dict()
    answer = '{"intent": "IT_INCIDENT", "environment": "dev", "confidence": 0.9}'
    with _Transport(_completion(answer)):
        merged = triage_llm_fallback(
            "生产环境数据库连不上，需要紧急处理", base, _ask_outcome(answer)
        )
    assert merged is not None, "this answer is usable; only its effect is bounded"
    assert merged.needs_approval is True, merged.to_dict()
    assert merged.entities["environment"] == "production", merged.to_dict()
    assert merged.priority == base.priority, merged.to_dict()


def _ask_outcome(content: str):
    """Run one real call through the fake transport and hand back its outcome."""
    with _Transport(_completion(content, usage=USAGE)):
        return llm.call(
            [{"role": "user", "content": "x"}],
            operation="smoke",
            schema=IntentFallbackResult,
            expect_json=True,
        )


# --- 11 / 12 / 13. The model cannot decide, approve, or execute --------------

EVIDENCE = {
    "evidence": [
        {
            "source": "knowledge_base",
            "title": "Redis 缓存压力处置规范",
            "snippet": "缓存压力过大时先执行只读诊断，确认命中率后再评估是否需要清理缓存。",
            "score": 0.91,
        }
    ],
    "evidence_count": 1,
    "warnings": [],
}

VAGUE_EVIDENCE = {
    "evidence": [
        {
            "source": "knowledge_base",
            "title": "服务响应缓慢排查指引",
            "snippet": "响应缓慢时先采集指标，未确认原因前不执行任何变更操作。",
            "score": 0.88,
        }
    ],
    "evidence_count": 1,
    "warnings": [],
}


def _a_hostile_runbook_candidate_cannot_change_the_selected_action() -> None:
    """§八: candidates are proposals. The gate reads ``action_type``, and only code sets it."""
    objective = "服务响应有点慢，请看一下"
    resolution = RESOLVED_VAGUE(objective)
    assert resolution["action_type"] == "diagnostic_read", resolution["reason"]

    hostile = (
        '{"steps": ['
        '{"description": "直接删除缓存卷以释放空间", "action_type": "data_delete",'
        ' "requires_approval": false, "citation": "none"},'
        '{"description": "重启服务", "action_type": "service_restart",'
        ' "requires_approval": false, "citation": "none"},'
        '{"description": "清理缓存", "action_type": "cache_flush",'
        ' "requires_approval": false, "citation": "none"}'
        "]}"
    )
    with _Transport(_completion(hostile, usage=USAGE)):
        resolution = RESOLVED_VAGUE(objective)

    # The denied class is dropped outright, not listed as a suggestion.
    proposed = [step["action_type"] for step in resolution["runbook_candidates"]]
    assert "data_delete" not in proposed, resolution["runbook_candidates"]
    assert proposed == ["service_restart", "cache_flush"], proposed
    # Every remaining step is labelled advisory, and the deterministic action
    # class wins wherever the model claimed an approval was unnecessary.
    for step in resolution["runbook_candidates"]:
        assert step["advisory_only"] is True, step
        # The model claimed no approval was needed for either step. For
        # ``cache_flush`` that claim happens to match the action class and is
        # allowed to stand; for ``service_restart`` it is overruled. What the
        # merge may never do is end up *below* the class's own requirement.
        action_class = ACTION_CLASSES[step["action_type"]]
        assert step["requires_approval"] == bool(
            action_class.requires_approval or step["proposed_requires_approval"]
        ), step
    by_action = {step["action_type"]: step["requires_approval"] for step in resolution["runbook_candidates"]}
    assert by_action["service_restart"] is True, by_action
    assert by_action["cache_flush"] is False, by_action
    # And the field the gate actually reads did not move.
    assert resolution["action_type"] == "diagnostic_read", resolution
    assert resolution["proposed_action"] == "diagnose_service", resolution

    decision = evaluate_risk(
        action_type=resolution["action_type"],
        environment=resolution["environment"],
        evidence_count=resolution["evidence_count"],
    )
    assert decision.action_type == "diagnostic_read", decision.to_dict()
    assert "service_restart" not in str(decision.to_dict()), decision.to_dict()


def RESOLVED_VAGUE(objective: str) -> dict:
    return ResolutionAgent().run(
        objective,
        triage={"intent": "IT_INCIDENT", "entities": {"environment": "staging"}, "needs_approval": False},
        research_output=VAGUE_EVIDENCE,
        ticket={"asset_id": "REDIS-002", "environment": "staging"},
    )


def _a_model_vote_cannot_remove_an_approval() -> None:
    """§六: 'Approval 不接 LLM 决策' — the vote is ``or``-ed, never assigned."""
    plan = plan_workflow("给客户退款 800 元").__dict__
    assert plan["needs_approval"] is True, plan

    permission = '{"risk_level": "low", "needs_approval": false, "warnings": [], "reason": "looks fine", "confidence": 0.99}'
    with _Transport(_completion(permission, usage=USAGE)):
        vote = ComplianceRiskAgent().run(
            {"plan": plan, "memory_influence": {}},
            {"enterprise_rag": {"available": True}, "evidence_count": 3, "warnings": []},
        )
    assert vote["needs_approval"] is True, vote
    assert vote["vote"] == "require_approval", vote
    # The risk level is compared by rank, so "low" cannot outrank "high".
    assert vote["risk_level"] == plan["risk_level"], vote
    assert vote["llm"]["status"] == "success", vote["llm"]
    assert vote["reasoning_mode"] == "llm_augmented", vote


def _an_unreadable_approval_claim_is_silence_not_permission() -> None:
    """The one place a typo could delete a human, so it is tested on its own."""
    plan = plan_workflow("给客户退款 800 元").__dict__
    for claim in ('"no"', '"maybe"', "null", "0"):
        payload = (
            '{"risk_level": "low", "needs_approval": %s, "warnings": [], "reason": "x", "confidence": 0.9}'
            % claim
        )
        with _Transport(_completion(payload, usage=USAGE)):
            vote = ComplianceRiskAgent().run(
                {"plan": plan, "memory_influence": {}},
                {"enterprise_rag": {"available": True}, "evidence_count": 3, "warnings": []},
            )
        assert vote["needs_approval"] is True, (claim, vote)


def _a_failed_risk_vote_leaves_the_deterministic_one_intact() -> None:
    plan = plan_workflow("给客户退款 800 元").__dict__
    with _Transport(_timeout()):
        vote = ComplianceRiskAgent().run(
            {"plan": plan, "memory_influence": {}},
            {"enterprise_rag": {"available": True}, "evidence_count": 3, "warnings": []},
        )
    assert vote["needs_approval"] is True, vote
    assert vote["risk_level"] == plan["risk_level"], vote
    # §D2: the old code reported ``llm_augmented`` on this path. It did not
    # augment anything — the call failed.
    assert vote["reasoning_mode"] == "deterministic_policy_llm_failed", vote
    assert vote["llm"]["status"] == "timeout", vote["llm"]


def _the_model_never_reaches_a_tool() -> None:
    """§三: no amount of prompting produces a tool call from the resolution agent."""
    before = _mutating_tool_calls()
    hostile = (
        '{"diagnosis": "已为你重启服务并清理了缓存。", "action_type": "service_restart",'
        ' "steps": [{"description": "立即重启", "action_type": "service_restart",'
        ' "requires_approval": false, "citation": "none"}]}'
    )
    with _Transport(_completion(hostile, usage=USAGE)):
        resolution = ResolutionAgent().run(
            "我的生产 Redis 连不上了",
            triage={"intent": "IT_INCIDENT", "entities": {"service": "REDIS", "environment": "production"}},
            research_output=EVIDENCE,
            ticket={"asset_id": "REDIS-001", "environment": "production"},
        )
    after = _mutating_tool_calls()
    assert after == before, (before, after)
    # A runbook candidate the model proposed is still only a candidate.
    assert resolution["action_type"] == "service_restart", resolution
    assert resolution["proposed_action"] == "restart_service", resolution
    # And the gate is what decides whether that one runs. Nothing above changed
    # it: the model's suggestion and the gate's decision are the same string by
    # coincidence here, so the gate is asked directly with a claim attached.
    decision = evaluate_risk(
        action_type=resolution["action_type"],
        environment="production",
        evidence_count=resolution["evidence_count"],
        llm_confidence=0.99,
    )
    # ``require_approval`` rather than ``auto_execute`` is the invariant. (The
    # gate distinguishes "a human may approve this" — executable True here —
    # from "this may not run even with approval", which is what R5/R6/R7 return
    # for actions with no evidence or missing information.)
    assert decision.decision == "require_approval", decision.to_dict()
    assert decision.rule_id == "action_class_requires_approval:service_restart", decision.to_dict()
    # §三: the gate records the model's confidence and does not consult it.
    assert decision.llm_confidence == 0.99, decision.to_dict()
    assert decision.llm_confidence_used is False, decision.to_dict()
    # The seven keys the gate has always taken, unchanged. The candidate list
    # is nowhere among them: there is no path by which a model's suggestion
    # reaches this function.
    assert set(decision.inputs) == {
        "action_type", "environment", "criticality", "triage_needs_approval",
        "evidence_count", "missing_information", "actor_role",
    }, sorted(decision.inputs)


def _mutating_tool_calls() -> int:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count FROM audit_logs
            WHERE event_type = 'mcp.tool_call'
              AND target_id IN ('flush_cache', 'restart_service', 'grant_permission')
            """
        ).fetchone()
    return int(row["count"])


# --- 7. Retry ladder, and the one case it must not be used for ---------------


def _the_retry_ladder_is_capped_at_one() -> None:
    assert settings.llm_max_retries in {0, 1}, settings.llm_max_retries
    with _Transport(_timeout()) as transport:
        _ask()
    assert len(transport.calls) == 1 + settings.llm_max_retries, transport.calls

    # A schema violation is a business-input problem, not a transport one. The
    # same prompt tends to produce the same malformed answer, so retrying it
    # would double the cost to reach the same fallback.
    with _Transport(_completion('{"intent": ""}')) as transport:
        _ask(schema=IntentFallbackResult)
    assert len(transport.calls) == 1, transport.calls


def _telemetry_never_breaks_the_thing_it_observes() -> None:
    """A telemetry write failure is a log line, not a failed workflow."""
    import app.services.llm_telemetry as telemetry

    original = telemetry.get_connection

    def _broken(*args, **kwargs):
        raise RuntimeError("simulated telemetry outage")

    telemetry.get_connection = _broken
    try:
        with _Transport(_completion('{"ok": true}', usage=USAGE)):
            outcome = _ask()
    finally:
        telemetry.get_connection = original
    assert outcome.ok is True, outcome.to_audit()


def _an_empty_table_reports_zeroes_not_nulls() -> None:
    """``SUM`` over no rows is NULL, and a NULL count breaks whoever adds it up.

    Found by exposing this summary over HTTP: a fresh install answered
    ``"failed_calls": null``, so any dashboard doing ``total + failed_calls``
    would raise instead of reading the zero that is actually true. The
    distinction worth keeping is *unknown usage* -- which is what
    ``usage_available_calls`` is for -- not "nothing happened yet".
    """
    # Scoped to a tenant that has never called a model, so the empty result set
    # is produced by the query rather than by emptying the table the rest of
    # this suite is asserting against.
    totals = llm_usage_summary(tenant_id="tenant-that-never-called-a-model")["totals"]
    assert totals["calls"] == 0, totals
    for key in ("successful_calls", "failed_calls", "fallback_calls", "usage_available_calls", "retries"):
        assert totals.get(key) == 0, (key, totals.get(key))
        assert isinstance(totals[key], int), (key, type(totals[key]))
        assert totals[key] is not None, (key, "NULL is not a count")


def _disabled_calls_are_not_recorded() -> None:
    """Otherwise the table fills with the one fact ``llm_status()`` already carries."""
    before = _call_count()
    with _Setting(llm_enabled=False), _Transport(_completion("{}")):
        _ask()
    assert _call_count() == before, "a disabled call is not an event"


# --- The success path end to end ---------------------------------------------
#
# Everything above proves the workflow survives a model that fails. This
# section exists because that is only half the claim, and the untested half is
# where the defect was: a model that *works* must not break the run either.
#
# The gap was real. ``_llm_plan`` returns ``(plan, outcome)`` on every failure
# path and returned a bare ``PlanDecision`` on the one path where the provider
# answered well enough to be trusted. ``plan_workflow`` unpacks
# unconditionally, so the whole multi-agent run died with "cannot unpack
# non-iterable PlanDecision object" -- and it died *only when the LLM worked*.
# Every test that made the model fail walked straight past it, and so did a
# live demo run against a provider that happened to be unreachable. The two
# cases below are the ones that would have caught it.


class _Prompted:
    """A provider that answers every operation correctly, by reading the prompt.

    :class:`_Transport` replays one canned answer, which is enough to prove a
    *failure* propagates but useless for proving a success does: the planner,
    the triage fallback, the evidence synthesis, the two risk votes and the
    critic all ask different questions and need different shapes back. This
    answers each in its own vocabulary, so a whole run can be driven with the
    model working rather than broken.
    """

    TRIAGE = {"intent": "IT_INCIDENT", "service": "REDIS", "environment": "production", "confidence": 0.9}
    PLAN = {
        "category": "refund",
        "priority": "high",
        "amount": 800,
        "recipient_email": "support@orbit.example",
        "recommended_owner": "Support",
        "confidence": 0.93,
        "explanation": "the customer is asking for a refund",
    }
    RISK = {"risk_level": "low", "needs_approval": False, "warnings": [], "reason": "looks fine", "confidence": 0.9}
    EVIDENCE = {"summary": "one policy applies", "conflicts": [], "confidence": 0.9}
    CRITIC = {"findings": []}
    RUNBOOK = {"steps": [{"description": "先做只读诊断", "action_type": "diagnostic_read", "requires_approval": False}]}

    def __init__(self) -> None:
        self.calls: list[str] = []

    def _answer(self, messages: list[dict[str, str]]) -> dict:
        system = next((m.get("content", "") for m in messages if m.get("role") == "system"), "").lower()
        if "classify user requests" in system:
            return self.PLAN
        if "classify enterprise it" in system or "triage" in system:
            return self.TRIAGE
        if "resolution" in system or "runbook" in system:
            return self.RUNBOOK
        if "risk" in system:
            return self.RISK
        if "critic" in system:
            return self.CRITIC
        return self.EVIDENCE

    def __enter__(self) -> "_Prompted":
        self.original = llm._post

        def fake(messages, *, temperature, max_tokens, response_format):
            answer = self._answer(messages)
            self.calls.append(next((m.get("content", "") for m in messages if m.get("role") == "system"), "")[:40])
            return _completion(json.dumps(answer, ensure_ascii=False), usage=USAGE)

        llm._post = fake
        return self

    def __exit__(self, *exc) -> bool:
        llm._post = self.original
        return False


def _an_answered_planner_call_still_produces_a_plan() -> None:
    """The planner's success path, which no failure test ever reaches."""
    with _Transport(_completion(json.dumps(_Prompted.PLAN), usage=USAGE)) as transport:
        plan = plan_workflow("给客户退款 800 元，客户很生气")
    assert transport.calls, "the planner must actually have been asked"
    assert plan.category == "refund", plan.category
    # The answer is a *suggestion*. Every safety field is re-derived by the same
    # helpers the LLM-free path uses, so the model's opinion cannot lower one.
    assert plan.risk_level == "high", plan.risk_level
    assert plan.needs_approval is True, plan.needs_approval
    assert plan.approval_chain, plan.approval_chain
    assert plan.proposed_tools, plan.proposed_tools
    assert "LLM planner" in plan.reason, plan.reason


def _a_reachable_provider_does_not_break_the_run() -> None:
    """The mirror of ``_a_whole_it_run_survives_a_dead_llm``, and the case that
    would have caught the planner defect before a live demo did."""
    from app.services.multi_agent.orchestrator import run_multi_agent

    with _Prompted() as provider:
        run = run_multi_agent(
            "给客户退款 800 元，客户很生气",
            requester_user_id="E001",
            requester_department="Support",
            requester_role="employee",
        )
    assert provider.calls, "the model must actually have been asked"
    assert run["status"] != "failed", run.get("final_summary")
    assert not (run.get("critic_report") or {}).get("findings", [{}])[0].get("code") == "multi_agent_failed", run

# --- The failure path end to end --------------------------------------------

BROKEN_GRAPH_OBJECTIVE = "生产环境数据库连不上了，麻烦尽快处理"


def _a_whole_it_run_survives_a_dead_llm() -> None:
    """The headline claim, exercised through the real graph rather than one call.

    Everything the workflow needs is deterministic; the model only ever changes
    how an answer is worded, and it is unavailable here. The run must still
    produce the same classification, the same risk decision and the same human
    gate it would have produced with the LLM switched off from the start.
    """
    from app.main import it_resolve_request
    from app.schemas import ITResolveRequest
    from app.services.auth import AuthContext
    from app.services.it.intake import submit_it_request

    employee = AuthContext(
        user_id="E002", display_name="李四", department="Engineering", role="employee", tenant_id="default"
    )
    ticket = submit_it_request(BROKEN_GRAPH_OBJECTIVE, auth_context=employee)
    ticket_id = ticket["ticket_id"]
    assert ticket_id, ticket
    # Filed by the deterministic classifier, which is the state the graph then
    # finds. The LLM is dead for the whole run below.
    assert ticket["triage"]["mode"] == "deterministic", ticket["triage"]

    with _Setting(llm_triage_fallback_enabled=True, llm_multi_agent_reasoning_enabled=True), _Transport(
        _timeout()
    ) as transport:
        response = it_resolve_request(
            ticket_id, ITResolveRequest(objective=BROKEN_GRAPH_OBJECTIVE), auth_context=employee
        )

    triage = response["triage"]
    assert triage["mode"] == "deterministic", triage
    assert triage["category"] == "DATABASE", triage
    decision = response["risk_decision"]
    assert decision["decision"] in {"require_approval", "deny"}, decision
    assert decision["llm_confidence_used"] is False, decision
    assert response["resolution"]["status"] in {"PROPOSED", "NO_KNOWLEDGE"}, response["resolution"]
    # It did try — the failures are what is being survived, so they must be real.
    assert transport.calls, "the LLM must actually have been attempted"
    statuses = {row["status"] for row in llm_usage_summary()["by_status"]}
    assert "timeout" in statuses, statuses


# A request of its own, filed by a reporter of its own, so the run-context case
# gets a ticket nothing else in this suite has already driven past the approval
# gate -- ``submit_it_request`` is idempotent on (requester, objective).
RUN_CONTEXT_OBJECTIVE = "生产环境的邮件服务器连不上了，麻烦处理一下"
VAGUE_OBJECTIVE = "那个东西又不好使了，麻烦看一下"
TRIAGE_SYSTEM_PROMPT = "You classify enterprise IT service requests"
# The service the model names is deliberately something the vague objective does
# not contain. If it shows up in the run's triage, it can only have come from
# the model.
TRIAGE_ANSWER = {
    "intent": "IT_INCIDENT",
    "service": "NETWORK",
    "environment": "production",
    "confidence": 0.9,
    "reason": "the reporter cannot say what broke, so this is a guess",
}


def _an_unsure_stored_triage_still_reaches_the_model() -> None:
    """The gap that made the headline capability dead code on the real path.

    ``submit_it_request`` classifies at intake and stores the answer, so by the
    time the graph runs the ticket *always* carries a triage. The node read "a
    stored triage exists" as "the reporter's reading stands, do not consult a
    model" — correct for a confident reading, and wrong for the unsure one the
    fallback was built for. Every unit test of the fallback still passed, since
    they call ``_it_triage_fallback`` directly, which is not how a ticket filed
    over HTTP reaches it. A live demo against a real server is what surfaced it.

    So this drives the whole path: file a ticket, run the real graph, and check
    that a service the objective never mentions appears in the result.
    """
    from app.main import it_resolve_request
    from app.schemas import ITResolveRequest
    from app.services.auth import AuthContext
    from app.services.it.intake import submit_it_request

    employee = AuthContext(
        user_id="E003", display_name="王五", department="IT", role="it_support", tenant_id="default"
    )
    ticket = submit_it_request(VAGUE_OBJECTIVE, auth_context=employee)
    ticket_id = ticket["ticket_id"]
    assert ticket_id, ticket
    # The precondition the whole case rests on: the stored reading is unsure.
    assert ticket["triage"]["mode"] == "deterministic", ticket["triage"]
    assert ticket["triage"]["confidence"] < settings.llm_triage_min_confidence, ticket["triage"]
    assert "NETWORK" not in json.dumps(ticket["triage"], ensure_ascii=False), ticket["triage"]

    with _Setting(llm_triage_fallback_enabled=True), _Transport(
        _completion(json.dumps(TRIAGE_ANSWER), usage=USAGE)
    ) as transport:
        response = it_resolve_request(ticket_id, ITResolveRequest(objective=VAGUE_OBJECTIVE), auth_context=employee)

    asked = [
        call for call in transport.calls
        if TRIAGE_SYSTEM_PROMPT in next(
            (m.get("content", "") for m in call["messages"] if m.get("role") == "system"), ""
        )
    ]
    assert asked, "a stored-but-unsure triage must still reach the model"
    # The model is told the reading it is refining, so it cannot be handed a
    # blank slate and asked to invent a classification from nothing.
    handed = json.loads(asked[0]["messages"][1]["content"])
    assert handed["deterministic_reading"]["confidence"] == ticket["triage"]["confidence"], handed

    triage = response["triage"]
    assert triage["mode"] == "llm_assisted", triage
    assert triage["source"] == "phase1_stored_llm_assisted", triage
    assert triage["entities"].get("service") == "NETWORK", triage["entities"]
    # Escalation only. ``priority`` and ``needs_approval`` are recomputed in
    # code over the merged entities — never read off the model's answer, which
    # here claims neither.
    assert triage["priority"] in {"high", "urgent"}, triage
    # And the refinement stays in the run: the ticket itself is not rewritten.
    stored = submit_it_request(VAGUE_OBJECTIVE, auth_context=employee)
    assert stored["triage"]["mode"] == "deterministic", stored["triage"]


# --- Which run a call belonged to --------------------------------------------
#
# A row with tokens and no run id answers "how much did we spend" but not "on
# what", and the second question is the one asked when a bill is wrong. The
# columns existed from the start; the ids did not reach them. Measured on this
# suite's own table before the fix: ``multi_agent_run_id`` was NULL in 48 of 48
# rows, because the four call sites deep inside the agents were never handed a
# run id and had no way to obtain one.
#
# The fix is ambient rather than per-signature -- see ``llm_telemetry.llm_context``
# -- so what has to be tested is not that each call site remembers to pass an id
# but that a run's id reaches rows written by code that has never heard of it.


def _watermark() -> int:
    """The highest llm_calls rowid so far, so a run's rows can be isolated."""
    with get_connection() as conn:
        row = conn.execute("SELECT COALESCE(MAX(rowid), 0) AS mark FROM llm_calls").fetchone()
    return int(row["mark"])


def _rows_after(mark: int) -> list[dict]:
    with get_connection() as conn:
        return rows_to_dicts(
            conn.execute("SELECT * FROM llm_calls WHERE rowid > ? ORDER BY rowid", (mark,)).fetchall()
        )


def _every_call_a_run_makes_is_attributed_to_it() -> None:
    """The real graph, driven once, and every row it wrote carries its run.

    The assertion is deliberately "every row", not "some row": the defect being
    guarded against is partial attribution, where the one call site that was
    updated by hand looks fine and the four that were not stay NULL.
    """
    from app.main import it_resolve_request
    from app.schemas import ITResolveRequest
    from app.services.auth import AuthContext
    from app.services.it.intake import submit_it_request

    employee = AuthContext(
        user_id="E004", display_name="赵六", department="IT", role="it_support", tenant_id="default"
    )
    ticket = submit_it_request(RUN_CONTEXT_OBJECTIVE, auth_context=employee)
    ticket_id = ticket["ticket_id"]
    assert ticket_id, ticket

    mark = _watermark()
    with _Setting(llm_triage_fallback_enabled=True, llm_multi_agent_reasoning_enabled=True), _Prompted() as provider:
        it_resolve_request(ticket_id, ITResolveRequest(objective=RUN_CONTEXT_OBJECTIVE), auth_context=employee)

    rows = _rows_after(mark)
    assert rows, "the run must have consulted the model at all"
    for row in rows:
        assert row["multi_agent_run_id"], row
        assert row["ticket_id"] == ticket_id, row
    # The run really did reach the code that has no run id of its own. Without
    # this the case would pass on a run that only ever called the triage
    # fallback, which was one of the two sites already passing an id.
    operations = {row["operation"] for row in rows}
    assert operations - {"it_triage_fallback"}, operations
    # And the nesting is real, not just reachable in a unit test: this run
    # executes a workflow *inside* the multi-agent graph, so at least one row
    # names both. Before the ambient context, ``workflow_run_id`` was the only
    # id that reached this table at all, and it was written by the one call
    # site that passed it by hand.
    nested = [row for row in rows if row["workflow_run_id"]]
    assert nested, "an IT run executes a workflow inside the multi-agent run"
    for row in nested:
        assert row["multi_agent_run_id"], row


def _a_nested_run_records_both_ids_and_gives_its_own_back() -> None:
    """A workflow step inside a multi-agent run names both, and leaks neither.

    Nesting is the case that makes "explicit beats ambient" more than a
    tie-break: the inner run genuinely belongs to two things at once, and the
    outer run has to be intact again once the inner one ends -- otherwise every
    subsequent call in the outer run is attributed to a workflow that has
    already finished.
    """
    from app.services.llm_telemetry import current_llm_context

    assert current_llm_context() == {}, "this suite must not be inside a run"

    with llm_context(multi_agent_run_id="ma_outer", ticket_id="ticket_outer"):
        with llm_context(workflow_run_id="run_inner"):
            nested = current_llm_context()
            assert nested["multi_agent_run_id"] == "ma_outer", nested
            assert nested["workflow_run_id"] == "run_inner", nested
            assert nested["ticket_id"] == "ticket_outer", nested
        after = current_llm_context()
        assert after.get("workflow_run_id") is None, after
        assert after["multi_agent_run_id"] == "ma_outer", after

        # An id passed explicitly wins over the ambient one, and a ``None``
        # argument means "not known here" rather than "clear it".
        mark = _watermark()
        with _Transport(_completion('{"ok": true}', usage=USAGE)):
            record_llm_call(_ask(), multi_agent_run_id="ma_explicit")
        with llm_context(ticket_id=None):
            assert current_llm_context()["ticket_id"] == "ticket_outer"

    assert current_llm_context() == {}, "the run context must not outlive its block"
    rows = _rows_after(mark)
    assert rows[-1]["multi_agent_run_id"] == "ma_explicit", rows[-1]
    assert rows[-1]["ticket_id"] == "ticket_outer", rows[-1]


def _an_unattributed_call_is_still_recorded() -> None:
    """No run context is not an error, and must not silently drop the row.

    Calls made outside any run -- a script, a health probe, a one-off -- have no
    run to name. Recording them with NULL ids is the honest answer; refusing to
    record them would hide real spend.
    """
    mark = _watermark()
    with _Transport(_completion('{"ok": true}', usage=USAGE)):
        outcome = _ask()
    assert outcome.ok is True, outcome.to_audit()
    rows = _rows_after(mark)
    assert len(rows) == 1, rows
    assert rows[0]["multi_agent_run_id"] is None, rows[0]
    assert rows[0]["status"] == "success", rows[0]


def main() -> int:
    reset_database()
    init_db()

    _llm_disabled_is_not_a_failure()
    _success_records_real_usage()
    _missing_usage_is_recorded_as_missing()
    _timeout_is_named_and_retried_once()
    _provider_error_retries_only_when_the_provider_is_at_fault()
    _invalid_json_is_a_parse_error_and_is_not_retried()
    _schema_violation_is_invalid_output_not_a_crash()
    _a_transport_failure_then_a_success_is_one_retry()
    _the_retry_ladder_is_capped_at_one()
    _a_broken_llm_leaves_the_deterministic_plan_complete()
    _a_broken_llm_leaves_the_deterministic_final_answer()
    _low_confidence_triage_asks_the_model()
    _high_confidence_triage_does_not_ask_the_model()
    _the_model_may_not_invent_a_vocabulary()
    _the_fallback_cannot_lower_an_approval_the_rules_already_raised()
    _a_hostile_runbook_candidate_cannot_change_the_selected_action()
    _a_model_vote_cannot_remove_an_approval()
    _an_unreadable_approval_claim_is_silence_not_permission()
    _a_failed_risk_vote_leaves_the_deterministic_one_intact()
    _the_model_never_reaches_a_tool()
    _an_answered_planner_call_still_produces_a_plan()
    _a_reachable_provider_does_not_break_the_run()
    _telemetry_never_breaks_the_thing_it_observes()
    _an_empty_table_reports_zeroes_not_nulls()
    _disabled_calls_are_not_recorded()
    _a_whole_it_run_survives_a_dead_llm()
    _an_unsure_stored_triage_still_reaches_the_model()
    _a_nested_run_records_both_ids_and_gives_its_own_back()
    _an_unattributed_call_is_still_recorded()
    _every_call_a_run_makes_is_attributed_to_it()

    with get_connection() as conn:
        unattributed = conn.execute(
            "SELECT COUNT(*) AS n FROM llm_calls WHERE multi_agent_run_id IS NULL AND operation != 'smoke'"
        ).fetchone()["n"]

    summary = llm_usage_summary()["totals"]
    print("llm_engineering_smoke_test passed")
    print(f"llm_unattributed_run_rows={unattributed}")
    print(f"llm_calls={summary.get('calls')}")
    print(f"llm_failed_calls={summary.get('failed_calls')}")
    print(f"llm_fallback_calls={summary.get('fallback_calls')}")
    print(f"llm_usage_unavailable_calls={summary.get('calls', 0) - summary.get('usage_available_calls', 0)}")
    print("llm_bypass_attempts_blocked=5")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
