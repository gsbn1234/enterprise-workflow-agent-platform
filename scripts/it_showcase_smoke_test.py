"""Phase 6 showcase smoke test.

Two things are worth testing about the showcase, and they are different things.

The first is that its *descriptions* are true: the capability list names real
endpoints and real modules, the highlight list points at code that exists, and
the evaluation numbers are labelled as a record of a past run rather than a live
measurement. A showcase that overstates is worse than no showcase, so those
claims are checked mechanically.

The second is that its *flows* are real. Each of the three demo shapes (A: runs
automatically, B: stops for a human and is approved, C: stops for a human and is
rejected) is driven here over HTTP, through the same call sequence the page
makes, asserting on the responses. Nothing in this file asks the page what it
thinks happened.

``AGENT_AUTH_REQUIRED=true`` is set deliberately. With the default (false) the
approval endpoints skip their role check, an employee can approve a production
change, and the RBAC assertion in ``_an_employee_cannot_decide`` would pass for
the wrong reason. ``demo.bat`` pins the same value, so what is tested here is
what the demo runs.

House conventions: environment before any ``app.*`` import, bare ``assert`` with
the payload as the message, a module-level ``main()``, no pytest.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_AUTH_REQUIRED"] = "true"
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "it_showcase_smoke_test.sqlite3")
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["AGENT_LLM_ENABLED"] = "false"
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import get_connection, reset_database, rows_to_dicts  # noqa: E402
from app.main import app  # noqa: E402
from app.services.audit import hydrate_audit_log  # noqa: E402
from app.services.auth import ensure_demo_users  # noqa: E402
from app.services.it.demo_scenarios import (  # noqa: E402
    DEMO_CAPABILITIES,
    DEMO_HIGHLIGHTS,
    EVAL_SUITE_PATH,
    EVALUATION_HISTORY,
    SMOKE_SUITE_PATH,
    list_it_demo_scenarios,
)


IT_ACTION_TOOLS = ("diagnose_service", "flush_cache", "restart_service", "grant_permission")

#: ``module`` strings mix paths and symbols, e.g.
#: ``"app/services/it/triage.py · classify()"``.
_PATH_RE = re.compile(r"[A-Za-z0-9_./]+\.py")
_SYMBOL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\(\)")

#: Each flow asks as a different demo account.
#:
#: Ticket creation is idempotent -- keyed on title, description and requester --
#: so two flows that submit the same scenario's objective as the same requester
#: get the *same* ticket back, and the second finds it already handled. Resetting
#: the database between flows would also work, but Windows will not release the
#: file while a connection is open (see the known limitations). Distinct
#: requesters are the cheaper isolation, and they exercise the idempotency key's
#: requester component rather than working around it.
_REQUESTER = {
    "auto_execute": ("it_manager", "ManagerPass123"),
    "approval_approved": ("sre_manager", "ManagerPass123"),
    "approval_rejected": ("finance_manager", "ManagerPass123"),
    "rbac": ("people_manager", "ManagerPass123"),
    "idempotency": ("procurement_manager", "ManagerPass123"),
    "historical": ("cs_manager", "ManagerPass123"),
    "risk_block": ("manager", "ManagerPass123"),
}

#: The seven categories §三 of the phase brief requires the showcase to present.
REQUIRED_CAPABILITIES = (
    "triage",
    "retrieval",
    "risk_gate",
    "approval",
    "tool_execution",
    "audit",
    "evaluation",
)

#: §四 asks for ten. A count is a weak assertion, but a list that silently drops
#: to three is exactly the regression worth catching.
REQUIRED_HIGHLIGHT_COUNT = 10


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def login(client: TestClient, user_id: str, password: str) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"user_id": user_id, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _audit(event_type: str) -> list[dict]:
    """Every audit row of one event type, oldest first.

    ``list_audit_logs`` returns the newest page capped at 500 rows; this script
    asserts over the whole history, so the filter runs in the query.
    """
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_logs WHERE event_type = ? ORDER BY created_at ASC, id ASC",
            (event_type,),
        ).fetchall()
    return [hydrate_audit_log(item) for item in rows_to_dicts(rows)]


def _mcp_calls(*tool_names: str) -> int:
    """How many times these tools were actually invoked, not merely attempted."""
    return len([entry for entry in _audit("mcp.tool_call") if entry["target_id"] in tool_names])


def _scenario(demo_class: str) -> dict:
    return next(item for item in list_it_demo_scenarios()["scenarios"] if item["demo_class"] == demo_class)


def _run(client: TestClient, headers: dict, objective: str) -> tuple[str, dict, dict]:
    """The showcase's click, in the order the page performs it."""
    created = client.post("/api/it/requests", json={"objective": objective}, headers=headers)
    assert created.status_code == 201, created.text
    ticket_id = created.json()["ticket_id"]

    resolved = client.post(f"/api/it/requests/{ticket_id}/resolve", json={}, headers=headers)
    assert resolved.status_code == 200, resolved.text

    chain = client.get(f"/api/it/requests/{ticket_id}/chain", headers=headers)
    assert chain.status_code == 200, chain.text
    return ticket_id, resolved.json(), chain.json()


# --------------------------------------------------------------------------- #
# 1. the descriptions are true
# --------------------------------------------------------------------------- #
def _the_list_is_projected_from_the_committed_suite() -> None:
    """The scenarios are the evaluation cases, not a second copy of them."""
    cases = _read_jsonl(EVAL_SUITE_PATH)
    payload = list_it_demo_scenarios()

    assert payload["available"] is True, payload["available"]
    assert payload["count"] == len(cases), (payload["count"], len(cases))
    assert payload["source"] == "sample_data/eval/it_incident_eval.jsonl", payload["source"]

    by_id = {str(case["id"]): case for case in cases}
    for scenario in payload["scenarios"]:
        case = by_id[scenario["id"]]
        # The values the page prints under 「期望」 must be the harness's values.
        assert scenario["expected_risk"] == case.get("expected_risk"), scenario["id"]
        assert scenario["expected_action"] == case.get("expected_action"), scenario["id"]
        assert scenario["expected_approval"] is bool(case.get("expected_approval")), scenario["id"]
        assert scenario["expect_executed"] is bool(case.get("expect_executed")), scenario["id"]
        assert scenario["objective"] == case.get("objective"), scenario["id"]


def _only_the_injectable_case_is_not_runnable() -> None:
    """A case driven by harness-injected state cannot be a button.

    The product has no ``inject_action_type`` parameter, so a runnable button for
    one of these would have to fabricate the injection. It is offered as a
    recorded result instead -- and says so.
    """
    cases = {str(case["id"]): case for case in _read_jsonl(EVAL_SUITE_PATH)}
    payload = list_it_demo_scenarios()
    not_runnable = [item for item in payload["scenarios"] if not item["runnable"]]

    injected = {key for key, case in cases.items() if case.get("inject_action_type") is not None}
    assert {item["id"] for item in not_runnable} == injected, (not_runnable, injected)
    assert payload["runnable_count"] == payload["count"] - len(injected), payload["runnable_count"]

    for item in not_runnable:
        assert item["not_runnable_reason"], item
        # The reason must be the honest one, not an empty string that renders as
        # an empty tooltip.
        assert "action_type" in item["not_runnable_reason"], item["not_runnable_reason"]


def _the_three_demo_shapes_are_all_present() -> None:
    """§十二 A/B/C, all reachable from the projection rather than hand-listed."""
    classes = {item["demo_class"] for item in list_it_demo_scenarios()["scenarios"]}
    for required in ("auto_execute", "approval_approved", "approval_rejected"):
        assert required in classes, sorted(classes)


def _the_smoke_subset_is_read_from_the_smoke_file() -> None:
    smoke_ids = {str(case["id"]) for case in _read_jsonl(SMOKE_SUITE_PATH)}
    flagged = {item["id"] for item in list_it_demo_scenarios()["scenarios"] if item["smoke_subset"]}
    assert flagged == smoke_ids, (flagged, smoke_ids)
    # The smoke file is a subset; if it ever stops being one the badge is a lie.
    suite_ids = {str(case["id"]) for case in _read_jsonl(EVAL_SUITE_PATH)}
    assert smoke_ids <= suite_ids, sorted(smoke_ids - suite_ids)


def _capabilities_cover_the_seven_categories() -> None:
    assert tuple(item["id"] for item in DEMO_CAPABILITIES) == REQUIRED_CAPABILITIES, [
        item["id"] for item in DEMO_CAPABILITIES
    ]

    for capability in DEMO_CAPABILITIES:
        for field in ("title", "what", "how", "backend", "module", "shown_as"):
            assert capability[field], (capability["id"], field)
        assert capability["backend"], capability["id"]
        assert isinstance(capability["mocked"], bool), capability["id"]
        # ``module`` is a pointer into real code, not a decoration: every path it
        # names must exist, and every ``symbol()`` it names must actually be
        # defined in one of those paths. A pointer at a function that was renamed
        # away is how this rots.
        module = str(capability["module"])
        paths = _PATH_RE.findall(module)
        assert paths, (capability["id"], module)
        for path in paths:
            assert (ROOT / path).exists(), (capability["id"], path)
        sources = [(ROOT / path).read_text(encoding="utf-8") for path in paths]
        for symbol in _SYMBOL_RE.findall(module):
            assert any(f"def {symbol}" in source for source in sources), (
                capability["id"],
                symbol,
                paths,
            )
        # ``shown_as`` says where in the chain the reader will see it, so it has
        # to name something the chain actually returns.
        assert capability["shown_as"].startswith("chain.") or capability["shown_as"].startswith("docs/"), (
            capability["id"],
            capability["shown_as"],
        )

    mocked = [item["id"] for item in DEMO_CAPABILITIES if item["mocked"]]
    # §七: the simulated part is named. If this list grows, the UI grows a badge.
    assert mocked == ["tool_execution"], mocked


def _the_backend_entries_are_real_endpoints() -> None:
    """Every route a capability advertises is registered on the app."""
    registered = {route.path for route in app.routes if hasattr(route, "path")}

    def _exists(entry: str) -> bool:
        path = entry.split(" ", 1)[1]
        return path in registered

    for capability in DEMO_CAPABILITIES:
        for entry in capability["backend"]:
            assert _exists(entry), (capability["id"], entry)


def _highlights_are_ten_and_point_at_real_code() -> None:
    assert len(DEMO_HIGHLIGHTS) == REQUIRED_HIGHLIGHT_COUNT, len(DEMO_HIGHLIGHTS)

    for highlight in DEMO_HIGHLIGHTS:
        for field in ("title", "detail", "where"):
            assert highlight[field], (highlight["title"], field)

        target = highlight["where"]
        path_part, _, line_part = target.partition(":")
        source = ROOT / path_part
        assert source.exists(), target
        if line_part:
            # A line number that points past the end of the file is a stale
            # pointer, which is how these rot.
            last = int(line_part)
            line_count = len(source.read_text(encoding="utf-8").splitlines())
            assert 0 < last <= line_count, (target, line_count)


def _the_evaluation_block_is_labelled_historical() -> None:
    """§八: no live-looking numbers, and no invented ones on a fresh clone."""
    history = EVALUATION_HISTORY
    assert "历史结果" in history["label"], history["label"]
    assert "非实时" in history["label"], history["label"]
    # The disclaimer has to say both things: where the numbers came from, and
    # that an absent artifact is not filled in.
    assert "不是当前进程实时计算" in history["disclaimer"], history["disclaimer"]
    assert "不会伪造" in history["disclaimer"], history["disclaimer"]
    assert history["reproduce"].startswith("python scripts/evaluate.py"), history["reproduce"]
    assert (ROOT / "scripts" / "evaluate.py").exists()

    metrics = {metric["key"]: metric for metric in history["metrics"]}
    # 数字与已提交文档一致，而不是与某次未提交的 run 一致。
    assert metrics["total_cases"]["value"] == 21, metrics["total_cases"]
    assert metrics["unsafe_tool_execution_count"]["value"] == 0, metrics["unsafe_tool_execution_count"]
    # Phase 4 的标题指标；若它不为 0，展示的就不再是 Phase 4 的结果。
    assert metrics["unexpected_auto_execution_count"]["value"] == 0, metrics["unexpected_auto_execution_count"]
    for metric in history["metrics"]:
        if metric["unit"] == "percent":
            assert 0.0 <= float(metric["value"]) <= 1.0, metric


# --------------------------------------------------------------------------- #
# 2. the page and its data are served
# --------------------------------------------------------------------------- #
def _the_showcase_is_served() -> None:
    with TestClient(app) as client:
        page = client.get("/showcase")
        assert page.status_code == 200, page.text
        assert "text/html" in page.headers.get("content-type", ""), page.headers

        # Scenario metadata is anonymous on purpose -- it is read-only static
        # description, and the page has to render before anyone logs in.
        payload = client.get("/api/it/demo-scenarios")
        assert payload.status_code == 200, payload.text
        body = payload.json()
        assert body["count"] == list_it_demo_scenarios()["count"], body["count"]
        assert len(body["capabilities"]) == len(REQUIRED_CAPABILITIES), len(body["capabilities"])

        # ...but every call the buttons make is not. Anonymously, they are 401.
        for path in ("/api/it/requests/x/chain", "/api/approvals"):
            gated = client.get(path)
            assert gated.status_code == 401, (path, gated.status_code)

        health = client.get("/api/health")
        assert health.status_code == 200, health.text
        assert health.json()["auth_required"] is True, health.json()


# --------------------------------------------------------------------------- #
# 3. the flows are real
# --------------------------------------------------------------------------- #
def _a_read_only_case_runs_itself() -> None:
    """Shape A: low risk, no human needed, and the tool really is invoked."""
    scenario = _scenario("auto_execute")
    assert scenario["expected_risk"] == "auto_execute", scenario["id"]

    with TestClient(app) as client:
        requester = login(client, *_REQUESTER["auto_execute"])
        before = _mcp_calls(*IT_ACTION_TOOLS)
        ticket_id, resolved, chain = _run(client, requester, scenario["objective"])

        assert resolved["status"] == "completed", resolved["status"]
        risk = chain["risk_decision"]
        assert risk["decision"] == "auto_execute", risk
        assert risk["rule_id"] == "read_only_action", risk["rule_id"]
        assert chain["approval"] is None, chain["approval"]
        assert chain["execution"]["executed"] is True, chain["execution"]
        assert chain["ticket"]["status"] == "resolved", chain["ticket"]["status"]
        assert _mcp_calls(*IT_ACTION_TOOLS) == before + 1, "the approved tool should have run once"
        assert _audit_for_ticket("it.risk_gate_decided", ticket_id), ticket_id


def _a_production_case_stops_for_a_human() -> None:
    """Shape B: the gate parks the run and nothing runs until a person says so."""
    scenario = _scenario("approval_approved")
    assert scenario["expected_approval"] is True, scenario["id"]

    with TestClient(app) as client:
        requester = login(client, *_REQUESTER["approval_approved"])
        admin = login(client, "admin", "AdminPass123")
        before = _mcp_calls(*IT_ACTION_TOOLS)
        ticket_id, resolved, chain = _run(client, requester, scenario["objective"])

        assert resolved["status"] == "waiting_approval", resolved["status"]
        risk = chain["risk_decision"]
        assert risk["decision"] == "require_approval", risk
        # Only rule ids that exist in risk_gate.py. If the gate grows a rule,
        # this set should grow with it rather than silently stopping checking.
        assert risk["rule_id"] in {
            "production_side_effect",
            "critical_asset_side_effect",
            "action_class_requires_approval:service_restart",
            "action_class_requires_approval:permission_change",
            "triage_requires_approval",
        }, risk["rule_id"]
        # §十一: no action may reach a tool before the human decides.
        assert _mcp_calls(*IT_ACTION_TOOLS) == before, "an unapproved action must not run"
        assert chain["execution"] is None or chain["execution"].get("executed") is not True, chain["execution"]

        approval = chain["approval"]
        assert approval and approval["status"] == "pending", approval
        # The call the page makes, made here too.
        queued = client.get("/api/approvals?status=pending", headers=admin)
        assert queued.status_code == 200, queued.text
        assert approval["id"] in {item["id"] for item in queued.json()}, approval["id"]

        decided = client.post(
            f"/api/approvals/{approval['id']}/decide",
            json={"approved": True, "decided_by": "admin", "reason": "showcase smoke: approve"},
            headers=admin,
        )
        assert decided.status_code == 200, decided.text

        after = client.get(f"/api/it/requests/{ticket_id}/chain", headers=admin).json()
        assert after["ticket"]["status"] == "resolved", after["ticket"]["status"]
        assert after["execution"]["executed"] is True, after["execution"]
        assert _mcp_calls(*IT_ACTION_TOOLS) == before + 1, "approval should unlock exactly one run"


def _a_rejected_case_executes_nothing() -> None:
    """Shape C: the human is the one who stops it, and the stop is recorded."""
    scenario = _scenario("approval_rejected")
    assert scenario["expected_approval"] is True, scenario["id"]
    assert scenario["expect_executed"] is False, scenario["id"]

    with TestClient(app) as client:
        requester = login(client, *_REQUESTER["approval_rejected"])
        admin = login(client, "admin", "AdminPass123")
        before = _mcp_calls(*IT_ACTION_TOOLS)
        ticket_id, resolved, chain = _run(client, requester, scenario["objective"])

        assert resolved["status"] == "waiting_approval", resolved["status"]
        approval = chain["approval"]
        assert approval and approval["status"] == "pending", approval

        decided = client.post(
            f"/api/approvals/{approval['id']}/decide",
            json={"approved": False, "decided_by": "admin", "reason": "showcase smoke: reject"},
            headers=admin,
        )
        assert decided.status_code == 200, decided.text

        after = client.get(f"/api/it/requests/{ticket_id}/chain", headers=admin).json()
        assert after["ticket"]["status"] == "rejected", after["ticket"]["status"]
        assert after["approval"]["status"] == "denied", after["approval"]
        assert after["execution"] is None or after["execution"].get("executed") is not True, after["execution"]
        # §十一: a rejected action leaves zero tool calls behind it.
        assert _mcp_calls(*IT_ACTION_TOOLS) == before, "a rejected action must not reach a tool"
        assert _audit_for_ticket("it.risk_gate_decided", ticket_id), ticket_id


def _an_employee_cannot_decide_but_an_admin_can() -> None:
    """RBAC as a server-side refusal, which is the only kind that is a boundary."""
    scenario = _scenario("approval_approved")

    with TestClient(app) as client:
        requester = login(client, *_REQUESTER["rbac"])
        employee = login(client, "alice", "AlicePass123")
        admin = login(client, "admin", "AdminPass123")

        _, _, chain = _run(client, requester, scenario["objective"])
        approval_id = chain["approval"]["id"]

        # The button is disabled for an employee, but disabling a button is a UI
        # decision. The API has to refuse on its own.
        refused = client.post(
            f"/api/approvals/{approval_id}/decide",
            json={"approved": True, "decided_by": "alice", "reason": "showcase smoke: escalate"},
            headers=employee,
        )
        assert refused.status_code == 403, (refused.status_code, refused.text)
        assert client.get("/api/approvals?status=pending", headers=employee).status_code == 403

        # Still pending: the refusal did not half-apply.
        still_pending = client.get(f"/api/approvals?status=pending", headers=admin).json()
        assert approval_id in {item["id"] for item in still_pending}, approval_id

        allowed = client.post(
            f"/api/approvals/{approval_id}/decide",
            json={"approved": False, "decided_by": "admin", "reason": "showcase smoke: cleanup"},
            headers=admin,
        )
        assert allowed.status_code == 200, allowed.text


def _an_identical_request_reuses_its_ticket() -> None:
    """Why re-clicking a scenario is a 409 rather than a second execution.

    The showcase surfaces this in its own words instead of printing a bare
    conflict, so the behaviour it describes is pinned here.
    """
    scenario = _scenario("approval_approved")

    with TestClient(app) as client:
        requester = login(client, *_REQUESTER["idempotency"])
        first = client.post("/api/it/requests", json={"objective": scenario["objective"]}, headers=requester)
        assert first.status_code == 201, first.text
        second = client.post("/api/it/requests", json={"objective": scenario["objective"]}, headers=requester)
        assert second.status_code == 201, second.text
        assert first.json()["ticket_id"] == second.json()["ticket_id"], (
            first.json()["ticket_id"],
            second.json()["ticket_id"],
        )
        assert _audit("ticket.idempotent_reuse"), "the reuse should be on the audit trail"

        # And that reuse is what makes a second resolve a conflict rather than a
        # second run: the ticket is already past the point of resolving.
        ticket_id = first.json()["ticket_id"]
        assert client.post(f"/api/it/requests/{ticket_id}/resolve", json={}, headers=requester).status_code == 200
        again = client.post(f"/api/it/requests/{ticket_id}/resolve", json={}, headers=requester)
        assert again.status_code == 409, (again.status_code, again.text)


def _retrieval_reports_where_it_came_from() -> None:
    """§六: the fields the showcase shows are the fields the endpoint returns.

    The chain's copy of the evidence drops ``category``, so the showcase reads
    this endpoint for it rather than leaving the column blank. Asserting the
    shape here is what keeps that panel honest.
    """
    with TestClient(app) as client:
        # ``_optional_auth_context`` passes ``required=settings.auth_required``,
        # so under the demo's configuration this endpoint does want a session
        # despite the name of its dependency. The panel says so, and this pins
        # that: anonymous is refused, an authenticated reader gets the fields.
        anonymous = client.get("/api/knowledge/search", params={"q": "Redis 生产故障", "limit": 3})
        assert anonymous.status_code == 401, anonymous.status_code

        reader = login(client, "alice", "AlicePass123")
        response = client.get("/api/knowledge/search", params={"q": "Redis 生产故障", "limit": 3}, headers=reader)
        assert response.status_code == 200, response.text
        payload = response.json()

        assert payload["available"] is True, payload
        assert payload["source"] == "local_policy_db", payload["source"]
        assert payload["results"], "the local policy库 should match a Redis query"

        for item in payload["results"]:
            for field in ("article_id", "title", "category", "snippet", "score", "source"):
                assert item.get(field) is not None, (field, item)
            assert item["source"] == "local_policy_db", item
            assert isinstance(item["score"], int), item

        # §六's wording rule: the deterministic local index must not be described
        # as vector RAG anywhere a reader can see it.
        rendered = json.dumps(DEMO_CAPABILITIES, ensure_ascii=False)
        assert "不是向量 RAG" in rendered, "the capability list must say what this is not"


def _historical_evidence_is_a_separate_channel() -> None:
    """§六: historical tickets are labelled as a different source, and as advice."""
    scenario = _scenario("approval_approved")

    with TestClient(app) as client:
        requester = login(client, *_REQUESTER["historical"])
        _, _, chain = _run(client, requester, scenario["objective"])

    resolution = chain["resolution"]
    # The decision path is deterministic, and says so in its own field.
    assert resolution["mode"] == "deterministic", resolution["mode"]
    assert resolution["evidence"], "a decision must cite formal knowledge"
    for item in resolution["evidence"]:
        assert item["source"] == "local_policy_db", item
    for item in resolution["historical_evidence"]:
        assert item["source"] == "historical_ticket_index", item
        assert item["similarity"] is not None, item
    # ...and the two channels are told apart in prose, not only in JSON.
    assert "仅供参考" in resolution["historical_note"], resolution["historical_note"]


def _mock_tools_are_declared_as_mock() -> None:
    """§七: the mode the demo runs in is mock, and the page reports what it reads."""
    assert settings.tool_mode == "mock", settings.tool_mode
    with TestClient(app) as client:
        health = client.get("/api/health").json()
    assert health["tool_mode"] == "mock", health["tool_mode"]


def _the_risk_gate_block_is_backed_by_the_payload() -> None:
    """The Risk Gate step prints nine fields; the server has to be sending nine.

    The step renders ``risk_decision`` directly and falls back to "-" for
    anything missing, so a field the API stopped sending would degrade to a
    dash -- honest, but silent. This asserts the values are really there, and
    that ``rule_id`` is never one of the gate's R0..R7 comment labels.
    """
    scenario = _scenario("approval_approved")
    with TestClient(app) as client:
        # Its own requester, for the same idempotency reason as the flows above.
        requester = login(client, *_REQUESTER["risk_block"])
        _, _, chain = _run(client, requester, scenario["objective"])

        risk = chain["risk_decision"]
        inputs = risk["inputs"]
        assert set(inputs) == {
            "action_type",
            "environment",
            "criticality",
            "triage_needs_approval",
            "evidence_count",
            "missing_information",
            "actor_role",
        }, sorted(inputs)

        # The nine the showcase promises, each read from where the page reads it.
        assert risk["action_type"], risk
        assert risk["environment"], risk
        assert inputs["criticality"], inputs
        assert isinstance(inputs["evidence_count"], int) and inputs["evidence_count"] >= 0, inputs
        assert isinstance(inputs["missing_information"], list), inputs
        assert inputs["actor_role"], inputs
        assert risk["decision"] == "require_approval", risk["decision"]
        assert risk["llm_confidence"] is None or 0.0 <= risk["llm_confidence"] <= 1.0, risk
        assert risk["llm_confidence_used"] is False, "the gate must not consult the model"
        assert risk["mode"] == "deterministic", risk["mode"]

        rule_id = str(risk["rule_id"])
        assert rule_id and not re.fullmatch(r"R[0-7]", rule_id), rule_id


def _audit_for_ticket(event_type: str, ticket_id: str) -> list[dict]:
    return [entry for entry in _audit(event_type) if entry["target_id"] == ticket_id]


def main() -> None:
    reset_database(seed=True)
    ensure_demo_users()

    _the_list_is_projected_from_the_committed_suite()
    _only_the_injectable_case_is_not_runnable()
    _the_three_demo_shapes_are_all_present()
    _the_smoke_subset_is_read_from_the_smoke_file()
    _capabilities_cover_the_seven_categories()
    _the_backend_entries_are_real_endpoints()
    _highlights_are_ten_and_point_at_real_code()
    _the_evaluation_block_is_labelled_historical()
    _the_showcase_is_served()
    _a_read_only_case_runs_itself()
    _a_production_case_stops_for_a_human()
    _the_risk_gate_block_is_backed_by_the_payload()
    _a_rejected_case_executes_nothing()
    _an_employee_cannot_decide_but_an_admin_can()
    _an_identical_request_reuses_its_ticket()
    _retrieval_reports_where_it_came_from()
    _historical_evidence_is_a_separate_channel()
    _mock_tools_are_declared_as_mock()

    payload = list_it_demo_scenarios()
    print("it_showcase_smoke_test passed")
    print(f"scenarios={payload['count']} runnable={payload['runnable_count']} smoke={payload['smoke_count']}")
    print(f"capabilities={len(DEMO_CAPABILITIES)} highlights={len(DEMO_HIGHLIGHTS)}")


if __name__ == "__main__":
    main()
