"""Retrieval over the historical IT ticket corpus.

This is the second of the two evidence channels the resolution agent reads, and
it exists as a separate module over a separate table on purpose. The formal
channel — ``app.services.tools.knowledge.search_knowledge`` over
``knowledge_articles`` — carries approved policy and runbooks. This one carries
*what actually happened last time*: one engineer's handling of one incident,
recorded after the fact and never reviewed as policy. A resolution may follow
the first and must only note the second, which is a distinction the platform
enforces by keeping the two in different tables, returning them under different
keys, and never letting the second reach ``_select_action``.

The retrieval itself is deliberately the cheapest thing that works: the same
deterministic keyword scorer the knowledge channel uses, over one small table,
with no embedding, no vector store and no new dependency. It is allowed to miss
a paraphrase, and it is not allowed to invent a match — a query whose words do
not appear in the corpus returns nothing, which is the answer callers act on.

``similarity`` is a ratio against the best score *this query* could have
achieved, not against the best score any row did achieve. That makes it a
statement about the row ("this row contains most of what you asked for") rather
than about the result set ("this is the best of a bad bunch"), so a query with
no real match reports low similarity instead of reporting 1.0 for the least
irrelevant row.
"""

from __future__ import annotations

from typing import Any

from app.db import get_connection, rows_to_dicts
from app.services.tools.knowledge import _extract_terms
from app.utils import compact_text

SOURCE = "historical_ticket_index"
MODE = "deterministic_keyword"

DEFAULT_LIMIT = 3
"""§五's K. Three is what the resolution context was specified around."""

MAX_LIMIT = 10

SNIPPET_LENGTH = 240


def search_historical_tickets(
    query: str,
    limit: int = DEFAULT_LIMIT,
    *,
    ticket_id: str | None = None,
    category: str | None = None,
    tenant_id: str | None = None,
    auth_context: Any = None,
) -> dict[str, Any]:
    """Find past IT tickets resembling ``query``, best match first.

    ``ticket_id`` is an exact fetch rather than a search: it returns that one
    row with whatever score it happens to earn, so "open the ticket this result
    came from" works even when the caller's query text shares no term with it.

    ``auth_context`` is accepted and unused. The tool spec leaves
    ``require_auth_context`` false — the graph nodes that call this hold a
    ``requester_user_id`` string, not an ``AuthContext`` — so tenant scoping is
    the caller's explicit ``tenant_id``, exactly as ``query_tickets`` does it.
    An omitted ``tenant_id`` therefore searches every tenant, which is why every
    production caller passes one.
    """
    text = str(query or "").strip()
    terms = _extract_terms(text)
    wanted_ticket = str(ticket_id or "").strip() or None
    wanted_category = str(category or "").strip().upper() or None
    safe_limit = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))

    if not terms and not wanted_ticket:
        # No searchable term and nothing named outright. This is a real answer,
        # not a failure: ``available`` stays true so a caller can tell "the
        # corpus held nothing for these words" apart from "the corpus is
        # missing", and the two lead to different behaviour upstream.
        return _empty(text, safe_limit, terms, reason="no_searchable_term")

    clauses: list[str] = []
    params: list[object] = []
    if wanted_ticket:
        clauses.append("ticket_id = ?")
        params.append(wanted_ticket)
    if wanted_category:
        clauses.append("upper(category) = ?")
        params.append(wanted_category)
    if tenant_id:
        clauses.append("tenant_id = ?")
        params.append(tenant_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT * FROM it_historical_tickets {where} ORDER BY ticket_id", params
        ).fetchall()

    lowered_query = text.lower()
    ceiling = 2 * len(terms)
    scored: list[tuple[int, dict[str, Any]]] = []
    for row in rows_to_dicts(rows):
        score = _score(row, terms, lowered_query)
        if score <= 0 and not wanted_ticket:
            continue
        scored.append((score, row))

    # Ties break on ticket id so the same query always returns the same order —
    # an evaluation that cannot reproduce its own retrieval cannot be trusted
    # about anything else.
    scored.sort(key=lambda item: (-item[0], item[1]["ticket_id"]))

    results = [
        {
            "ticket_id": row["ticket_id"],
            "title": row["title"],
            "description": compact_text(row["description"], SNIPPET_LENGTH),
            "resolution": compact_text(row["resolution"], SNIPPET_LENGTH),
            "category": row["category"],
            "environment": row["environment"],
            "asset_id": row["asset_id"],
            "status": row["status"],
            "resolution_action": row["resolution_action"],
            "score": score,
            "similarity": _similarity(score, ceiling),
            "source": SOURCE,
        }
        for score, row in scored[:safe_limit]
    ]
    return {
        "query": text,
        "limit": safe_limit,
        "count": len(results),
        "matched_count": len(scored),
        "source": SOURCE,
        "available": True,
        "mode": MODE,
        "terms": sorted(terms),
        "results": results,
    }


def _score(row: dict[str, Any], terms: set[str], lowered_query: str) -> int:
    """Weighted keyword overlap, shaped like ``search_knowledge``'s scorer.

    A term in the title is worth double a term in the body: titles are what the
    service desk wrote to be found again. The category bonus mirrors
    ``search_knowledge`` as well, and is what lets "内网 DNS 解析失败" pull the
    ``NETWORK`` tickets even though the reporter never said the word.
    """
    title = str(row["title"] or "").lower()
    haystack = " ".join(
        str(row.get(field) or "")
        for field in ("ticket_id", "title", "category", "description", "resolution", "environment")
    ).lower()
    score = 0
    for term in terms:
        if term in title:
            score += 2
        elif term in haystack:
            score += 1
    if str(row.get("category") or "").lower() in lowered_query:
        score += 2
    return score


def _similarity(score: int, ceiling: int) -> float:
    """Fraction of the best achievable score for this query, clamped to [0, 1].

    The clamp is real: the category bonus is added on top of a full term sweep,
    so a row can score above a ceiling computed from terms alone.
    """
    if ceiling <= 0:
        return 0.0
    return round(max(0.0, min(1.0, score / ceiling)), 4)


def _empty(query: str, limit: int, terms: set[str], *, reason: str) -> dict[str, Any]:
    return {
        "query": query,
        "limit": limit,
        "count": 0,
        "matched_count": 0,
        "source": SOURCE,
        "available": True,
        "mode": MODE,
        "terms": sorted(terms),
        "results": [],
        "reason": reason,
    }
