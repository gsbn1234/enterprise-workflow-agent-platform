"""Phase 3 historical ticket retrieval: the second evidence channel, kept second.

    "我的生产 Redis 连不上了"
      -> knowledge retrieval (policy, runbooks)   <- the agent may act on this
      -> historical retrieval (what happened last time)  <- it may only cite this

Four things are checked, and the last two are the ones that matter:

1. The retrieval finds the past ticket it should, with a score and a source.
2. A query the corpus cannot answer returns *nothing* rather than the least
   irrelevant row, and "found nothing" stays distinguishable from "unavailable".
3. When formal knowledge answers the question, the resolution acts on that and
   still reports the history beside it - the two channels are separate answers,
   not one merged pile.
4. When the past handling *contradicts* current policy, the past handling is
   reported and not followed. The fixture is real: the top historical match for
   a production database lock was resolved in the past with a ``data_delete``,
   an action the current risk gate refuses outright.

Nothing here contacts a real system: mock providers, no RAG service, and the
deterministic corpora seeded into ``knowledge_articles`` and
``it_historical_tickets``. The LLM is off, because a retrieval channel that only
works when a model is guessing alongside it is not a retrieval channel.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
os.environ["AGENT_DB_PATH"] = str(ROOT / "data" / "it_history_smoke_test.sqlite3")
os.environ["KNOWLEDGE_RAG_BASE_URL"] = ""
os.environ["AGENT_TOOL_MODE"] = "mock"
os.environ["AGENT_TICKET_PROVIDER"] = "mock"
os.environ["AGENT_EMAIL_PROVIDER"] = "mock"
os.environ["AGENT_LLM_ENABLED"] = "false"
sys.path.insert(0, str(ROOT))

from app.db import get_connection, reset_database, rows_to_dicts  # noqa: E402
from app.main import it_resolve_request  # noqa: E402
from app.schemas import ITResolveRequest  # noqa: E402
from app.services.audit import hydrate_audit_log  # noqa: E402
from app.services.auth import AuthContext, ensure_demo_users  # noqa: E402
from app.services.it.history import SOURCE, search_historical_tickets  # noqa: E402
from app.services.it.intake import submit_it_request  # noqa: E402
from app.services.tenancy import set_current_tenant_id  # noqa: E402
from app.utils import json_loads  # noqa: E402


EMPLOYEE = AuthContext(
    user_id="E002", display_name="李四", department="Engineering", role="employee", tenant_id="default"
)

# The past Redis outage whose handling is the reference for today's.
REDIS_QUERY = "生产 Redis 连不上"
EXPECTED_TOP = "IT-2025-1041"

# Nothing in either corpus contains these words. The canteen is not an IT system.
UNANSWERABLE = "今天食堂几点开门"

# The policy-conflict fixture. ``search_historical_tickets`` ranks
# IT-2025-1095 top for this sentence once the graph appends the triage entity
# codes to the query, and that ticket was closed with a ``data_delete``.
CONFLICT_INCIDENT = "生产数据库锁表，查询全部阻塞"
CONFLICT_FIXTURE = "IT-2025-1095"
CONFLICT_FIXTURE_ACTION = "data_delete"

HANDBOOK_TITLE = "Redis 生产故障排查手册"


def main() -> None:
    reset_database(seed=True)
    ensure_demo_users()
    # The resolve route normally gets this from its auth dependency; this script
    # calls the route function directly, so it sets the scope itself.
    set_current_tenant_id("default")

    # 1 + 2. The retrieval itself, hit and miss.
    _retrieval_finds_the_past_ticket()
    _retrieval_returns_nothing_rather_than_something()
    # 3. Formal knowledge decides; history is reported alongside it.
    _knowledge_decides_and_history_is_only_reported()
    # 4. History contradicts policy, and loses.
    _historical_precedent_does_not_override_policy()

    print("it_history_smoke_test passed")
    print(f"history_rows={_history_count()}")
    print(f"retrieved_top={EXPECTED_TOP}")
    print(f"conflict_fixture={CONFLICT_FIXTURE}")


# --------------------------------------------------------------------------- #
# 1. A hit, with a score and a source a caller can act on
# --------------------------------------------------------------------------- #


def _retrieval_finds_the_past_ticket() -> None:
    """The corpus holds what §四 asked it to hold, and the scorer finds it."""
    result = search_historical_tickets(REDIS_QUERY, 3, tenant_id="default")

    assert result["source"] == SOURCE, result
    assert result["available"] is True, result
    assert result["mode"] == "deterministic_keyword", result
    assert result["limit"] == 3, result
    assert result["terms"], result
    assert result["count"] >= 1, result
    # Every scored row is returned, not just the ones inside the limit, so a
    # caller can tell "three matched" apart from "three matched out of nine".
    assert result["matched_count"] >= result["count"], result

    top = result["results"][0]
    assert top["ticket_id"] == EXPECTED_TOP, result["results"]
    # §五's required fields, each one present and non-empty.
    assert top["title"], top
    assert top["description"], top
    assert top["resolution"], top
    assert top["category"] == "REDIS", top
    assert top["environment"] == "production", top
    assert top["status"], top
    assert top["resolution_action"] == "service_restart", top
    assert top["score"] > 0, top
    assert 0.0 < top["similarity"] <= 1.0, top
    assert top["source"] == SOURCE, top

    # Default K is the spec's K, and it is applied rather than declared.
    assert search_historical_tickets(REDIS_QUERY, tenant_id="default")["limit"] == 3
    assert len(search_historical_tickets(REDIS_QUERY, tenant_id="default")["results"]) <= 3

    # An exact fetch is a different question from a search: the named row comes
    # back even for a query that shares no term with it.
    named = search_historical_tickets("zzz 无关查询", 3, ticket_id=EXPECTED_TOP, tenant_id="default")
    assert named["count"] == 1, named
    assert named["results"][0]["ticket_id"] == EXPECTED_TOP, named["results"]

    # Tenant scoping is the tool's own SQL, not something upstream remembers.
    assert search_historical_tickets(REDIS_QUERY, 3, tenant_id="no-such-tenant")["count"] == 0

    # And the channel is reached inside a real run, not only from this script.
    incident = submit_it_request("我的生产 Redis 连接超时了", auth_context=EMPLOYEE)
    response = it_resolve_request(incident["ticket_id"], ITResolveRequest(), auth_context=EMPLOYEE)
    history = response["resolution"]["historical_evidence"]
    assert history, response["resolution"]
    assert history[0]["ticket_id"] == EXPECTED_TOP, history
    assert response["resolution"]["historical_count"] == len(history), response["resolution"]

    # The graph recorded it as a handoff with its own task key, separate from
    # the knowledge research that ran before it.
    retrieved = _audit("it.historical_retrieved", incident["ticket_id"])
    assert len(retrieved) == 1, retrieved
    detail = retrieved[0]["detail"]
    assert detail["ticket_id"] == incident["ticket_id"], detail
    assert detail["count"] >= 1, detail
    assert detail["available"] is True, detail
    assert detail["query"], detail
    assert detail["top"], detail
    assert detail["top"][0]["ticket_id"] == EXPECTED_TOP, detail["top"]

    message = _agent_message(response["multi_agent_run_id"], "historical_ticket_research")
    assert message, "the retrieval must be a recorded agent handoff"
    assert message["count"] >= 1, message
    assert message["results"][0]["ticket_id"] == EXPECTED_TOP, message


# --------------------------------------------------------------------------- #
# 2. A miss is an answer, and it is not the same answer as "broken"
# --------------------------------------------------------------------------- #


def _retrieval_returns_nothing_rather_than_something() -> None:
    """No term in the query appears in the corpus, so nothing is returned.

    The failure this guards against is the one every keyword scorer drifts into:
    ranking *something* because the caller asked for a ranking. Three rows would
    have been available to return here, and returning any of them would have put
    an unrelated past ticket in front of a human as if it were a lead.
    """
    result = search_historical_tickets(UNANSWERABLE, 3, tenant_id="default")

    assert result["count"] == 0, result
    assert result["results"] == [], result
    assert result["matched_count"] == 0, result
    # The distinction §十四-2 turns on: the corpus answered, and the answer was
    # "nothing". ``available`` says the channel worked; ``count`` says it found
    # nothing. Collapsing those two into one flag would make an empty corpus and
    # an empty result indistinguishable to every caller upstream.
    assert result["available"] is True, result
    assert result["terms"] == [], result
    assert result["reason"] == "no_searchable_term", result

    # A query made entirely of noise behaves the same way, and does not raise.
    assert search_historical_tickets("     ", 3, tenant_id="default")["count"] == 0

    # §五 caps K. Asked for a hundred rows or for none, a caller gets a number
    # inside the range rather than whatever it passed.
    assert search_historical_tickets(REDIS_QUERY, 99, tenant_id="default")["limit"] == 10
    assert search_historical_tickets(REDIS_QUERY, -3, tenant_id="default")["limit"] == 1
    assert search_historical_tickets(REDIS_QUERY, 0, tenant_id="default")["limit"] == 3
    assert len(search_historical_tickets(REDIS_QUERY, 10, tenant_id="default")["results"]) <= 10

    # A run over an unanswerable ticket still records that the channel was
    # consulted and came back empty, which is what lets the chain view show the
    # step rather than skip it.
    ticket = submit_it_request(UNANSWERABLE, auth_context=EMPLOYEE)
    response = it_resolve_request(ticket["ticket_id"], ITResolveRequest(), auth_context=EMPLOYEE)
    resolution = response["resolution"]
    assert resolution["historical_count"] == 0, resolution
    assert resolution["historical_evidence"] == [], resolution
    assert resolution["historical_reference"] is False, resolution
    assert resolution["historical_divergence"] is False, resolution
    # §七: nothing was found in either channel, so nothing is proposed.
    assert resolution["status"] == "NO_KNOWLEDGE", resolution
    assert resolution["action_type"] == "no_action", resolution
    assert response["execution"] is None, response["execution"]


# --------------------------------------------------------------------------- #
# 3. Knowledge decides. History is reported, and only reported.
# --------------------------------------------------------------------------- #


def _knowledge_decides_and_history_is_only_reported() -> None:
    """The normal case, and the one §六 is really about.

    Both channels return something here. The assertion is not that history was
    ignored - it was retrieved, counted and returned to the caller - but that it
    stayed out of the fields the decision is made from. ``evidence`` and
    ``evidence_count`` are what ``_select_action`` and the risk gate read, and a
    historical row appearing in either would be the exact failure §六 forbids.
    """
    ticket = submit_it_request("我的生产 Redis 服务器挂了，业务不可用", auth_context=EMPLOYEE)
    response = it_resolve_request(ticket["ticket_id"], ITResolveRequest(), auth_context=EMPLOYEE)
    resolution = response["resolution"]

    assert resolution["status"] == "PROPOSED", resolution
    assert resolution["evidence"], resolution
    # The separation, asserted on the data rather than trusted to the code path.
    assert all(item.get("source") != SOURCE for item in resolution["evidence"]), resolution["evidence"]
    assert all(item.get("ticket_id") is None for item in resolution["evidence"]), resolution["evidence"]
    assert HANDBOOK_TITLE in [item["title"] for item in resolution["evidence"]], resolution["evidence"]
    assert resolution["evidence_count"] == len(resolution["evidence"]), resolution

    # The same run did retrieve history, and says so - separately.
    assert resolution["historical_evidence"], resolution
    assert all(item["source"] == SOURCE for item in resolution["historical_evidence"]), resolution
    assert all(item["ticket_id"] for item in resolution["historical_evidence"]), resolution
    assert resolution["historical_count"] == len(resolution["historical_evidence"]), resolution
    # Formal knowledge was sufficient, so history is explicitly not the basis.
    assert resolution["historical_reference"] is False, resolution
    assert "仅供参考" in resolution["historical_note"], resolution["historical_note"]

    # The gate saw only the knowledge count, and acted on the action class.
    gate = response["risk_decision"]
    assert gate["rule_id"] == "action_class_requires_approval:service_restart", gate
    assert gate["inputs"]["evidence_count"] == resolution["evidence_count"], gate["inputs"]
    # History has no input channel into the risk decision at all. This is the
    # §六 guarantee stated as a schema fact rather than as a promise.
    assert set(gate["inputs"]) == {
        "action_type", "environment", "criticality", "triage_needs_approval",
        "evidence_count", "missing_information", "actor_role",
    }, gate["inputs"]


# --------------------------------------------------------------------------- #
# 4. A precedent that contradicts policy is reported, and does not win
# --------------------------------------------------------------------------- #


def _historical_precedent_does_not_override_policy() -> None:
    """§十一-5. The past handling is the wrong answer, and it is on the record.

    IT-2025-1095 closed a production database lock with a ``data_delete``. That
    action is in ``ACTION_CLASSES`` as destructive and is refused outright by
    the gate. If the historical channel had any influence on action selection,
    this is the run where it would show: the top hit proposes the forbidden
    action, and it is the only precedent the agent is shown.
    """
    ticket = submit_it_request(CONFLICT_INCIDENT, auth_context=EMPLOYEE)
    response = it_resolve_request(ticket["ticket_id"], ITResolveRequest(), auth_context=EMPLOYEE)
    resolution = response["resolution"]

    assert resolution["historical_evidence"], resolution
    assert resolution["historical_evidence"][0]["ticket_id"] == CONFLICT_FIXTURE, resolution[
        "historical_evidence"
    ]
    assert resolution["historical_evidence"][0]["resolution_action"] == CONFLICT_FIXTURE_ACTION, resolution

    # The divergence is flagged rather than silently averaged away: a human
    # reading the chain can see that the precedent and the proposal disagree.
    assert resolution["historical_divergence"] is True, resolution
    # The note says which of the two channels this decision came from, so a
    # reader who sees the divergence flag is told what it does and does not
    # mean. It is the same sentence every run with sufficient knowledge gets:
    # divergence adds a flag, never a different basis.
    assert "仅供参考" in resolution["historical_note"], resolution["historical_note"]

    # And the proposal is the deterministic production-critical one, not the
    # precedent. This is the assertion the whole case exists for.
    assert resolution["action_type"] != CONFLICT_FIXTURE_ACTION, resolution
    assert resolution["action_type"] == "service_restart", resolution
    assert resolution["proposed_action"] == "restart_service", resolution

    # The gate judged the proposed action, so the forbidden precedent never
    # reaches it: the run stops for a human rather than being denied or, worse,
    # executed.
    gate = response["risk_decision"]
    assert gate["decision"] == "require_approval", gate
    assert gate["rule_id"] != "denied_action_class:destructive", gate
    assert gate["tool_name"] == "restart_service", gate


# --------------------------------------------------------------------------- #
# Readers
# --------------------------------------------------------------------------- #


def _audit(event_type: str, ticket_id: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT * FROM audit_logs
            WHERE event_type = ? AND target_type = 'ticket' AND target_id = ?
            ORDER BY rowid ASC
            """,
            (event_type, ticket_id),
        ).fetchall()
    return [hydrate_audit_log(item) for item in rows_to_dicts(rows)]


def _agent_message(run_id: str, agent_name: str) -> dict:
    """The stored output of one agent handoff inside a run."""
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT content_json FROM multi_agent_messages
            WHERE run_id = ? AND agent_name = ?
            ORDER BY rowid DESC LIMIT 1
            """,
            (run_id, agent_name),
        ).fetchone()
    if not row:
        return {}
    content = json_loads(row["content_json"], {})
    return content if isinstance(content, dict) else {}


def _history_count() -> int:
    with get_connection() as conn:
        row = conn.execute("SELECT COUNT(*) AS total FROM it_historical_tickets").fetchone()
    return int(row["total"])


if __name__ == "__main__":
    main()
